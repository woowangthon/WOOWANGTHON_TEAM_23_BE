import difflib
import json
import logging
import threading
import time
import uuid

import httpx

from app.db import Database
from app.extract import find_page, squash

log = logging.getLogger("worker")
MAX_ATTEMPTS = 3
AVOID_LIMIT = 30
# 이 비율 이상 같은 문제 문장은 표현만 바꾼 같은 문제로 봄
SIMILAR_QUESTION = 0.85
RETRY_SECONDS = 5


class Worker:
    """문제 생성 작업자 1개. 모델이 한 번에 1건만 빠르게 처리하므로 순서대로 처리함 (노션 실험 기록 5)"""

    def __init__(self, db: Database, ai_url: str, timeout: float = 300):
        self.db = db
        self.ai_url = ai_url
        self.timeout = timeout
        self.wake = threading.Event()
        self.stopped = False

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self) -> None:
        self.stopped = True
        self.wake.set()

    def run(self) -> None:
        while not self.stopped:
            if not self.step():
                self.wake.wait(5)
                self.wake.clear()

    def next_chunk(self):
        # 조각 번호 순으로 고르면 자료마다 번갈아 처리되어 모든 자료가 첫 문제를 빨리 받음
        with self.db.lock:
            return self.db.conn.execute(
                "SELECT c.material_id, c.idx, c.text, c.attempts, m.device_id FROM chunks c "
                "JOIN materials m ON m.id = c.material_id WHERE c.status = 'pending' "
                "ORDER BY c.idx, m.created_at LIMIT 1"
            ).fetchone()

    def next_refill(self):
        with self.db.lock:
            return self.db.conn.execute(
                "SELECT r.rowid AS rid, r.material_id, r.chunk_idx, r.attempts, m.device_id, c.text FROM refills r "
                "JOIN materials m ON m.id = r.material_id JOIN chunks c ON c.material_id = r.material_id AND c.idx = r.chunk_idx "
                "WHERE r.status = 'pending' ORDER BY r.rowid LIMIT 1"
            ).fetchone()

    def refill_step(self) -> bool:
        """처음 올린 자료의 조각 처리가 끝난 뒤에 새 문제 채우기를 처리함"""
        refill = self.next_refill()
        if refill is None:
            return False
        with self.db.lock:
            # 이 조각에서 이미 낸 문제만 골라 프롬프트에 넣음. 자료 전체를 넣으면 너무 길어짐
            avoid = [
                row["question"] for row in self.db.conn.execute(
                    "SELECT question, evidence FROM questions WHERE material_id = ? ORDER BY created_at", (refill["material_id"],)
                ) if row["evidence"] and row["evidence"] in refill["text"]
            ][-AVOID_LIMIT:]
        try:
            r = httpx.post(f"{self.ai_url}/generate", json={"text": refill["text"], "avoid": avoid}, timeout=self.timeout)
            r.raise_for_status()
            questions = r.json()["questions"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("refill failed material=%s chunk=%s: %r", refill["material_id"], refill["chunk_idx"], e)
            with self.db.lock:
                status = "pending" if refill["attempts"] + 1 < MAX_ATTEMPTS else "failed"
                self.db.conn.execute(
                    "UPDATE refills SET attempts = attempts + 1, status = ? WHERE rowid = ?", (status, refill["rid"])
                )
            if status == "pending":
                time.sleep(RETRY_SECONDS)
            return True
        with self.db.lock:
            if self.db.conn.execute("SELECT 1 FROM materials WHERE id = ?", (refill["material_id"],)).fetchone():
                pages = [row["text"] for row in self.db.conn.execute(
                    "SELECT text FROM pages WHERE material_id = ? ORDER BY page", (refill["material_id"],)
                )]
                existing = self.db.conn.execute(
                    "SELECT type, question, answer, evidence FROM questions WHERE material_id = ?", (refill["material_id"],)
                ).fetchall()
                questions = drop_duplicates(questions, existing)
                self.db.conn.execute("BEGIN")
                self.insert_questions(refill["material_id"], refill["device_id"], questions, pages)
                self.db.conn.execute("UPDATE refills SET status = 'done' WHERE rowid = ?", (refill["rid"],))
                self.db.conn.execute("COMMIT")
        return True

    def step(self) -> bool:
        chunk = self.next_chunk()
        if chunk is None:
            return self.refill_step()
        try:
            r = httpx.post(f"{self.ai_url}/generate", json={"text": chunk["text"]}, timeout=self.timeout)
            r.raise_for_status()
            questions = r.json()["questions"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("generate failed material=%s chunk=%s: %r", chunk["material_id"], chunk["idx"], e)
            self.retry_or_fail(chunk)
            return True
        self.save(chunk, questions)
        return True

    def retry_or_fail(self, chunk) -> None:
        with self.db.lock:
            if chunk["attempts"] + 1 < MAX_ATTEMPTS:
                self.db.conn.execute(
                    "UPDATE chunks SET attempts = attempts + 1 WHERE material_id = ? AND idx = ?",
                    (chunk["material_id"], chunk["idx"]),
                )
            else:
                self.finish_chunk(chunk, "failed")
        if chunk["attempts"] + 1 < MAX_ATTEMPTS:
            time.sleep(RETRY_SECONDS)

    def save(self, chunk, questions: list[dict]) -> None:
        with self.db.lock:
            exists = self.db.conn.execute("SELECT 1 FROM materials WHERE id = ?", (chunk["material_id"],)).fetchone()
            if not exists:
                return
            pages = [row["text"] for row in self.db.conn.execute(
                "SELECT text FROM pages WHERE material_id = ? ORDER BY page", (chunk["material_id"],)
            )]
            self.db.conn.execute("BEGIN")
            self.insert_questions(chunk["material_id"], chunk["device_id"], questions, pages)
            self.finish_chunk(chunk, "done")
            self.db.conn.execute("COMMIT")

    def insert_questions(self, material_id: str, device_id: str, questions: list[dict], pages: list[str]) -> None:
        for q in questions:
            self.db.conn.execute(
                "INSERT INTO questions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"q_{uuid.uuid4().hex[:12]}", material_id, device_id, q["type"], q["question"],
                    q["answer"], json.dumps(q.get("accepted") or [], ensure_ascii=False), q["evidence"],
                    find_page(q["evidence"], pages), time.time(),
                ),
            )

    def finish_chunk(self, chunk, status: str) -> None:
        conn = self.db.conn
        conn.execute("UPDATE chunks SET status = ? WHERE material_id = ? AND idx = ?", (status, chunk["material_id"], chunk["idx"]))
        conn.execute("UPDATE materials SET chunks_done = chunks_done + 1 WHERE id = ?", (chunk["material_id"],))
        pending = conn.execute(
            "SELECT 1 FROM chunks WHERE material_id = ? AND status = 'pending' LIMIT 1", (chunk["material_id"],)
        ).fetchone()
        if not pending:
            count = conn.execute("SELECT COUNT(*) FROM questions WHERE material_id = ?", (chunk["material_id"],)).fetchone()[0]
            conn.execute("UPDATE materials SET status = ? WHERE id = ?", ("ready" if count else "failed", chunk["material_id"]))


def drop_duplicates(new: list[dict], existing) -> list[dict]:
    """이미 있는 문제와 같은 것을 뺌. 모델에 이미 낸 문제를 알려 줘도 표현만 바꿔 다시 묻는 경우가 있음"""
    seen = [
        (row["type"], squash(row["evidence"]), squash(row["answer"]).casefold(), squash(row["question"])) for row in existing
    ]
    kept = []
    for q in new:
        key = (q.get("type"), squash(q.get("evidence", "")), squash(q.get("answer", "")).casefold())
        text = squash(q.get("question", ""))
        same_fact = any(key == s[:3] for s in seen)
        similar = any(difflib.SequenceMatcher(None, text, s[3]).ratio() >= SIMILAR_QUESTION for s in seen)
        if same_fact or similar:
            continue
        kept.append(q)
        seen.append((*key, text))
    return kept
