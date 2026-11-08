import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.db import Database
from app.extract import UnsupportedFile, extract_pages, kind_of, split_chunks, squash
from app.worker import Worker

KST = timezone(timedelta(hours=9))
MAX_FILES = 5
MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
MAX_PROCESSING = 5
DAILY_UPLOADS = 10
# 전체 대기 조각 상한. 조각당 약 4초라 이 정도면 대기가 1시간을 넘음
MAX_PENDING_CHUNKS = 900


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(status, detail={"code": code, "message": message})


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(KST).date().isoformat()


def create_app(db_path: str | None = None, ai_url: str | None = None, run_worker: bool = True) -> FastAPI:
    db_path = db_path or os.environ.get("BE_DB_PATH", "data/be.sqlite3")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db = Database(db_path)
    worker = Worker(db, ai_url or os.environ.get("AI_URL", "http://localhost:8001/internal/v1"))

    @asynccontextmanager
    async def lifespan(_):
        if run_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.db = db
    app.state.worker = worker

    @app.exception_handler(HTTPException)
    def http_error(_, e: HTTPException):
        detail = e.detail if isinstance(e.detail, dict) else {"code": "not_found" if e.status_code == 404 else "invalid_request", "message": str(e.detail)}
        return JSONResponse({"error": detail}, status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    def validation_error(_, e: RequestValidationError):
        return JSONResponse({"error": {"code": "invalid_request", "message": str(e.errors())[:300]}}, status_code=400)

    def device(x_device_id: str | None) -> str:
        if not x_device_id or not x_device_id.strip():
            raise ApiError(400, "invalid_request", "X-Device-Id 헤더 필요")
        return x_device_id.strip()

    def use_quota(device_id: str, kind: str, amount: int, limit: int) -> None:
        conn = db.conn
        row = conn.execute(
            "SELECT count FROM usage_counts WHERE device_id = ? AND day = ? AND kind = ?", (device_id, today(), kind)
        ).fetchone()
        used = row["count"] if row else 0
        if used + amount > limit:
            raise ApiError(429, "rate_limited", f"하루 한도 초과 ({kind} {limit})")
        conn.execute(
            "INSERT INTO usage_counts VALUES (?, ?, ?, ?) ON CONFLICT DO UPDATE SET count = count + excluded.count",
            (device_id, today(), kind, amount),
        )

    def material_json(row) -> dict:
        count = db.conn.execute("SELECT COUNT(*) FROM questions WHERE material_id = ?", (row["id"],)).fetchone()[0]
        return {
            "id": row["id"], "name": row["name"], "status": row["status"],
            "chunksTotal": row["chunks_total"], "chunksDone": row["chunks_done"], "questionCount": count,
        }

    def question_json(row) -> dict:
        return {
            "id": row["id"], "materialId": row["material_id"], "type": row["type"], "question": row["question"],
            "answer": row["answer"], "accepted": json.loads(row["accepted"]), "evidence": row["evidence"], "page": row["page"],
        }

    def owned_question(device_id: str, question_id: str):
        row = db.conn.execute("SELECT * FROM questions WHERE id = ? AND device_id = ?", (question_id, device_id)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "없는 문제")
        return row

    @app.post("/api/v1/materials", status_code=202)
    async def upload(files: list[UploadFile] = File(...), x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        if not 1 <= len(files) <= MAX_FILES:
            raise ApiError(400, "invalid_request", f"파일은 1~{MAX_FILES}개")
        parsed = []
        for f in files:
            data = await f.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ApiError(413, "file_too_large", f"{f.filename}: 20MB 초과")
            try:
                pages = extract_pages(kind_of(f.filename or "", f.content_type), data)
            except UnsupportedFile:
                raise ApiError(415, "unsupported_file", f"{f.filename}: PDF·PPTX가 아니거나 열 수 없음")
            if len(pages) > MAX_PAGES:
                raise ApiError(413, "file_too_large", f"{f.filename}: {MAX_PAGES}쪽(슬라이드) 초과")
            chunks = split_chunks(pages)
            if not chunks:
                raise ApiError(415, "unsupported_file", f"{f.filename}: 글자를 뽑을 수 없음 (스캔본, 이미지 슬라이드)")
            parsed.append((f.filename or "자료", pages, chunks))

        with db.lock:
            conn = db.conn
            processing = conn.execute(
                "SELECT COUNT(*) FROM materials WHERE device_id = ? AND status = 'processing'", (device_id,)
            ).fetchone()[0]
            if processing + len(parsed) > MAX_PROCESSING:
                raise ApiError(429, "too_many_materials", f"동시 처리 자료는 {MAX_PROCESSING}개까지")
            pending = conn.execute("SELECT COUNT(*) FROM chunks WHERE status = 'pending'").fetchone()[0]
            if pending + sum(len(c) for _, _, c in parsed) > MAX_PENDING_CHUNKS:
                raise ApiError(429, "rate_limited", "생성 대기열이 가득 참")
            conn.execute("BEGIN")
            try:
                use_quota(device_id, "upload", len(parsed), DAILY_UPLOADS)
                ids = []
                for name, pages, chunks in parsed:
                    material_id = f"m_{uuid.uuid4().hex[:12]}"
                    ids.append(material_id)
                    conn.execute(
                        "INSERT INTO materials VALUES (?, ?, ?, 'processing', ?, 0, ?)",
                        (material_id, device_id, name, len(chunks), time.time()),
                    )
                    conn.executemany("INSERT INTO pages VALUES (?, ?, ?)", [(material_id, n, t) for n, t in enumerate(pages, 1)])
                    conn.executemany("INSERT INTO chunks (material_id, idx, text) VALUES (?, ?, ?)", [(material_id, i, t) for i, t in enumerate(chunks)])
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            rows = [conn.execute("SELECT * FROM materials WHERE id = ?", (i,)).fetchone() for i in ids]
            result = [material_json(r) for r in rows]
        worker.wake.set()
        return {"materials": result}

    @app.get("/api/v1/materials")
    def list_materials(x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        with db.lock:
            rows = db.conn.execute("SELECT * FROM materials WHERE device_id = ? ORDER BY created_at", (device_id,)).fetchall()
            return {"materials": [material_json(r) for r in rows]}

    @app.delete("/api/v1/materials/{material_id}", status_code=204)
    def delete_material(material_id: str, x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        with db.lock:
            conn = db.conn
            if not conn.execute("SELECT 1 FROM materials WHERE id = ? AND device_id = ?", (material_id, device_id)).fetchone():
                raise ApiError(404, "not_found", "없는 자료")
            conn.execute("BEGIN")
            for table in ("questions", "chunks", "pages"):
                conn.execute(f"DELETE FROM {table} WHERE material_id = ?", (material_id,))
            conn.execute("DELETE FROM materials WHERE id = ?", (material_id,))
            conn.execute("COMMIT")
        return Response(status_code=204)

    @app.get("/api/v1/questions")
    def list_questions(since: str | None = Query(None), x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        try:
            since_ts = datetime.fromisoformat(since).timestamp() if since else 0
        except ValueError:
            raise ApiError(400, "invalid_request", "since는 ISO 8601 시각")
        # 작업자와 같은 lock 안에서 시각을 정해야 serverTime 직전에 저장된 문제를 놓치지 않음
        with db.lock:
            server_time = datetime.now(KST)
            rows = db.conn.execute(
                "SELECT * FROM questions WHERE device_id = ? AND created_at > ? ORDER BY created_at", (device_id, since_ts)
            ).fetchall()
        return {"questions": [question_json(r) for r in rows], "serverTime": server_time.isoformat()}

    class GradeBody(BaseModel):
        userAnswer: str

    # 단답은 명사 하나로만 출제하고 같은 대상의 다른 이름을 accepted에 미리 받아 두므로 글자 비교만 함
    @app.post("/api/v1/questions/{question_id}/grade")
    def grade(question_id: str, body: GradeBody, x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        with db.lock:
            q = owned_question(device_id, question_id)
        if q["type"] != "short":
            raise ApiError(400, "invalid_request", "단답 문제만 채점함")
        answers = {squash(a).casefold() for a in [q["answer"], *json.loads(q["accepted"])] if a.strip()}
        return {"correct": squash(body.userAnswer).casefold() in answers, "by": "rule"}

    class Answer(BaseModel):
        questionId: str
        result: str
        answeredAt: str

    class AnswersBody(BaseModel):
        answers: list[Answer]

    @app.post("/api/v1/answers")
    def post_answers(body: AnswersBody, x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        recorded = 0
        with db.lock:
            for a in body.answers:
                if a.result not in ("correct", "wrong", "skipped"):
                    continue
                if not db.conn.execute("SELECT 1 FROM questions WHERE id = ? AND device_id = ?", (a.questionId, device_id)).fetchone():
                    continue
                db.conn.execute("INSERT INTO answers VALUES (?, ?, ?, ?)", (device_id, a.questionId, a.result, a.answeredAt))
                recorded += 1
        return {"recorded": recorded}

    @app.get("/api/v1/review")
    def review(x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        with db.lock:
            rows = db.conn.execute(
                "SELECT q.*, COUNT(*) AS wrong_count, MAX(a.answered_at) AS last_wrong_at FROM answers a "
                "JOIN questions q ON q.id = a.question_id WHERE a.device_id = ? AND a.result = 'wrong' "
                "GROUP BY q.id ORDER BY last_wrong_at DESC",
                (device_id,),
            ).fetchall()
        return {"items": [
            {"question": question_json(r), "wrongCount": r["wrong_count"], "lastWrongAt": r["last_wrong_at"]} for r in rows
        ]}

    class ReportBody(BaseModel):
        reason: str

    @app.post("/api/v1/questions/{question_id}/report", status_code=204)
    def report(question_id: str, body: ReportBody, x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        with db.lock:
            owned_question(device_id, question_id)
            db.conn.execute("INSERT INTO reports VALUES (?, ?, ?, ?)", (device_id, question_id, body.reason, time.time()))
        return Response(status_code=204)

    @app.get("/api/v1/health")
    def health():
        return {"ok": True, "time": now_iso()}

    return app

