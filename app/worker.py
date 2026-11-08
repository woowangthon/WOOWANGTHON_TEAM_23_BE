import json
import logging
import threading
import time
import uuid

import httpx

from app.db import Database
from app.extract import find_page

log = logging.getLogger("worker")
MAX_ATTEMPTS = 3
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

    def step(self) -> bool:
        chunk = self.next_chunk()
        if chunk is None:
            return False
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
            for q in questions:
                self.db.conn.execute(
                    "INSERT INTO questions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"q_{uuid.uuid4().hex[:12]}", chunk["material_id"], chunk["device_id"], q["type"], q["question"],
                        q["answer"], json.dumps(q.get("accepted") or [], ensure_ascii=False), q["evidence"],
                        find_page(q["evidence"], pages), time.time(),
                    ),
                )
            self.finish_chunk(chunk, "done")
            self.db.conn.execute("COMMIT")

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
