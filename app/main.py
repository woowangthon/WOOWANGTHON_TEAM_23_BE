import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db import Database
from app.extract import TooManyPages, UnsupportedFile, extract_pages, kind_of, split_chunks, squash
from app.worker import Worker

KST = timezone(timedelta(hours=9))
MAX_FILES = 5
MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
MAX_PROCESSING = 5
DAILY_UPLOADS = 10
# 전체 대기 조각 상한. 조각당 약 4초라 이 정도면 대기가 1시간을 넘음
MAX_PENDING_CHUNKS = 900
# 로그인이 없어 기기 ID를 바꾸면 기기별 한도를 피할 수 있음. 서버 전체와 IP 기준 하루 한도로 OpenAI 비용을 묶음
DAILY_CHUNKS = int(os.environ.get("BE_DAILY_CHUNKS", "2000"))
IP_DAILY_UPLOADS = int(os.environ.get("BE_IP_DAILY_UPLOADS", "30"))
# 안 푼 문제가 자료마다 이 수보다 적어지면 새 문제를 채움. 자료 하나에 채우는 횟수는 상한을 둠
FRESH_TARGET = 5
MAX_REFILLS_PER_MATERIAL = 10
MAX_BODY_BYTES = MAX_FILES * MAX_BYTES + 1024 * 1024
MAX_DEVICE_ID = 64


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

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.worker = worker

    @app.exception_handler(HTTPException)
    def http_error(_, e: HTTPException):
        detail = e.detail if isinstance(e.detail, dict) else {"code": "not_found" if e.status_code == 404 else "invalid_request", "message": str(e.detail)}
        return JSONResponse({"error": detail}, status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    def validation_error(_, e: RequestValidationError):
        return JSONResponse({"error": {"code": "invalid_request", "message": str(e.errors())[:300]}}, status_code=400)

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        # 파일별 20MB 검사는 본문을 다 받은 뒤라, 그 전에 전체 크기로 먼저 거름
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": {"code": "file_too_large", "message": "요청 크기 초과"}}, status_code=413)
        return await call_next(request)

    def device(x_device_id: str | None) -> str:
        if not x_device_id or not x_device_id.strip():
            raise ApiError(400, "invalid_request", "X-Device-Id 헤더 필요")
        if len(x_device_id.strip()) > MAX_DEVICE_ID:
            raise ApiError(400, "invalid_request", "X-Device-Id가 너무 김")
        return x_device_id.strip()

    def use_quota(device_id: str, kind: str, amount: int, limit: int, message: str | None = None) -> None:
        conn = db.conn
        row = conn.execute(
            "SELECT count FROM usage_counts WHERE device_id = ? AND day = ? AND kind = ?", (device_id, today(), kind)
        ).fetchone()
        used = row["count"] if row else 0
        if used + amount > limit:
            raise ApiError(429, "rate_limited", message or f"하루 한도 초과 ({kind} {limit})")
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

    def schedule_refills(device_id: str, question_ids: set[str]) -> None:
        """풀이 기록이 들어온 자료마다 안 푼 문제가 모자라면 새 문제 만들기를 예약함. lock 안에서 부름"""
        conn = db.conn
        if not question_ids:
            return
        marks = ",".join("?" * len(question_ids))
        material_ids = [r["material_id"] for r in conn.execute(
            f"SELECT DISTINCT material_id FROM questions WHERE device_id = ? AND id IN ({marks})", (device_id, *question_ids)
        )]
        for material_id in material_ids:
            material = conn.execute("SELECT * FROM materials WHERE id = ? AND status = 'ready'", (material_id,)).fetchone()
            if material is None:
                continue
            fresh = conn.execute(
                "SELECT COUNT(*) FROM questions q WHERE q.material_id = ? AND q.device_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM answers a WHERE a.question_id = q.id AND a.device_id = q.device_id)",
                (material_id, device_id),
            ).fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM refills WHERE material_id = ? AND status = 'pending'", (material_id,)).fetchone()[0]
            made = conn.execute("SELECT COUNT(*) FROM refills WHERE material_id = ?", (material_id,)).fetchone()[0]
            if pending or fresh >= FRESH_TARGET or made >= MAX_REFILLS_PER_MATERIAL:
                continue
            try:
                use_quota("*", "chunks", 1, DAILY_CHUNKS)
            except ApiError:
                continue
            # 조각을 돌아가며 골라 자료의 여러 부분에서 문제가 나오게 함
            conn.execute(
                "INSERT INTO refills (material_id, chunk_idx) VALUES (?, ?)", (material_id, made % material["chunks_total"])
            )
            worker.wake.set()

    def owned_question(device_id: str, question_id: str):
        row = db.conn.execute("SELECT * FROM questions WHERE id = ? AND device_id = ?", (question_id, device_id)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "없는 문제")
        return row

    @app.post("/api/v1/materials", status_code=202)
    async def upload(request: Request, files: list[UploadFile] = File(...), x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        if not 1 <= len(files) <= MAX_FILES:
            raise ApiError(400, "invalid_request", f"파일은 1~{MAX_FILES}개")
        parsed = []
        for f in files:
            data = await f.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ApiError(413, "file_too_large", f"{f.filename}: 20MB 초과")
            try:
                # 파싱이 오래 걸려도 다른 요청이 멈추지 않게 스레드에서 돌림
                pages = await run_in_threadpool(extract_pages, kind_of(f.filename or "", f.content_type), data, MAX_PAGES)
            except TooManyPages:
                raise ApiError(413, "file_too_large", f"{f.filename}: {MAX_PAGES}쪽(슬라이드) 초과")
            except UnsupportedFile:
                raise ApiError(415, "unsupported_file", f"{f.filename}: PDF·PPTX가 아니거나 열 수 없음")
            chunks = split_chunks(pages)
            if not chunks:
                raise ApiError(415, "unsupported_file", f"{f.filename}: 글자를 뽑을 수 없음 (스캔본, 이미지 슬라이드)")
            parsed.append(((f.filename or "자료")[:200], pages, chunks))

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
                # Fly 프록시가 붙이는 실제 접속 IP. 로컬 실행에는 없어서 건너뜀
                ip = request.headers.get("fly-client-ip")
                if ip:
                    use_quota(f"ip:{ip}", "upload", len(parsed), IP_DAILY_UPLOADS)
                use_quota("*", "chunks", sum(len(c) for _, _, c in parsed), DAILY_CHUNKS, "서버 하루 생성 한도 초과")
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
            # 풀이 결과와 신고도 자료와 함께 지움. 문제가 사라지면 쓸 곳이 없고, 삭제 안내 문구와 맞추기 위함
            for table in ("answers", "reports"):
                conn.execute(
                    f"DELETE FROM {table} WHERE question_id IN (SELECT id FROM questions WHERE material_id = ?)",
                    (material_id,),
                )
            for table in ("questions", "chunks", "pages", "refills"):
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
        userAnswer: str = Field(max_length=100)

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
        questionId: str = Field(max_length=40)
        result: str = Field(max_length=10)
        answeredAt: str = Field(max_length=40)

    class AnswersBody(BaseModel):
        answers: list[Answer] = Field(max_length=200)

    @app.post("/api/v1/answers")
    def post_answers(body: AnswersBody, x_device_id: str | None = Header(None)):
        device_id = device(x_device_id)
        recorded = 0
        touched: set[str] = set()
        with db.lock:
            for a in body.answers:
                if a.result not in ("correct", "wrong", "skipped"):
                    continue
                if not db.conn.execute("SELECT 1 FROM questions WHERE id = ? AND device_id = ?", (a.questionId, device_id)).fetchone():
                    continue
                # 앱이 기록을 다시 보내도 같은 기록이 쌓이지 않게 함
                if db.conn.execute(
                    "SELECT 1 FROM answers WHERE device_id = ? AND question_id = ? AND answered_at = ?",
                    (device_id, a.questionId, a.answeredAt),
                ).fetchone():
                    continue
                db.conn.execute("INSERT INTO answers VALUES (?, ?, ?, ?)", (device_id, a.questionId, a.result, a.answeredAt))
                touched.add(a.questionId)
                recorded += 1
            schedule_refills(device_id, touched)
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
        reason: str = Field(max_length=200)

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

