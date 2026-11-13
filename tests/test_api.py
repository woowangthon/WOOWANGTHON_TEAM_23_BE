import io

import pytest
from fastapi.testclient import TestClient
from pptx import Presentation
from pptx.util import Inches

from app import worker as worker_module
from app.main import create_app

H = {"X-Device-Id": "dev-1"}
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def make_pptx(slides: list[str]) -> bytes:
    prs = Presentation()
    for text in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5)).text_frame.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def make_pdf(text: str) -> bytes:
    """글꼴 하나와 글자 한 줄만 있는 PDF"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


SLIDES = [
    "도로교통법은 혈중알코올농도 0.03퍼센트 이상인 상태의 운전을 금지한다. " * 60,
    "운전면허 학과시험은 40문항이며 합격 기준은 1종 70점, 2종 60점이다. " * 20,
]


class FakeAI:
    def __init__(self):
        self.fail = False
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url, json))
        request = worker_module.httpx.Request("POST", url)
        if self.fail:
            return worker_module.httpx.Response(502, json={"error": {"code": "llm_unavailable"}}, request=request)
        text = json["text"]
        sentence = "운전면허 학과시험은 40문항이며 합격 기준은 1종 70점, 2종 60점이다." if "학과시험" in text else \
            "도로교통법은 혈중알코올농도 0.03퍼센트 이상인 상태의 운전을 금지한다."
        return worker_module.httpx.Response(200, json={"questions": [
            {"type": "ox", "question": sentence, "answer": "O", "accepted": [], "evidence": sentence},
            {"type": "short", "question": "단답 문제는 무엇인가?", "answer": "정답", "accepted": ["정답"], "evidence": sentence},
        ], "provider": "ollama", "discarded": 0}, request=request)


@pytest.fixture
def env(tmp_path, monkeypatch):
    ai = FakeAI()
    monkeypatch.setattr(worker_module.httpx, "post", ai.post)
    monkeypatch.setattr(worker_module, "RETRY_SECONDS", 0)
    app = create_app(str(tmp_path / "t.sqlite3"), "http://ai/internal/v1", run_worker=False)
    with TestClient(app) as client:
        yield client, app.state.worker, ai


def upload(client, files, headers=H):
    return client.post("/api/v1/materials", files=[("files", f) for f in files], headers=headers)


def drain(worker):
    while worker.step():
        pass


def test_upload_generate_and_download(env):
    client, worker, ai = env
    r = upload(client, [("면허.pptx", make_pptx(SLIDES), PPTX)])
    assert r.status_code == 202
    material = r.json()["materials"][0]
    assert material["status"] == "processing" and material["chunksTotal"] >= 1

    drain(worker)
    m = client.get("/api/v1/materials", headers=H).json()["materials"][0]
    assert m["status"] == "ready" and m["chunksDone"] == m["chunksTotal"] and m["questionCount"] > 0

    r = client.get("/api/v1/questions", headers=H).json()
    qs = r["questions"]
    assert len(qs) == m["questionCount"]
    assert {q["page"] for q in qs if q["evidence"].startswith("도로교통법")} == {1}
    assert {q["page"] for q in qs if q["evidence"].startswith("운전면허")} == {2}
    assert client.get("/api/v1/questions", params={"since": r["serverTime"]}, headers=H).json()["questions"] == []

    # 다른 기기에는 보이지 않음
    assert client.get("/api/v1/questions", headers={"X-Device-Id": "dev-2"}).json()["questions"] == []


def test_pdf_upload(env):
    client, worker, _ = env
    r = upload(client, [("a.pdf", make_pdf("Paging divides memory into fixed size pages. " * 30), "application/pdf")])
    assert r.status_code == 202
    assert worker.next_chunk()["text"].startswith("Paging")


def test_upload_errors(env):
    client, _, _ = env
    assert upload(client, [("a.hwp", b"x", "application/x-hwp")]).json()["error"]["code"] == "unsupported_file"
    assert upload(client, [("a.pdf", b"not a pdf", "application/pdf")]).status_code == 415
    assert upload(client, [("empty.pptx", make_pptx([""]), PPTX)]).status_code == 415
    r = upload(client, [("big.pptx", make_pptx(["x"] * 101), PPTX)])
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"
    assert upload(client, [("a.pptx", make_pptx(SLIDES), PPTX)], headers={}).status_code == 400


def test_limits(env):
    client, worker, _ = env
    data = make_pptx(SLIDES)
    assert upload(client, [(f"{i}.pptx", data, PPTX) for i in range(5)]).status_code == 202
    r = upload(client, [("6.pptx", data, PPTX)])
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_materials"
    drain(worker)
    assert upload(client, [(f"{i}.pptx", data, PPTX) for i in range(5)]).status_code == 202
    drain(worker)
    r = upload(client, [("11.pptx", data, PPTX)])
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


def test_ai_failure_marks_failed_after_retries(env):
    client, worker, ai = env
    ai.fail = True
    upload(client, [("a.pptx", make_pptx(SLIDES[:1]), PPTX)])
    drain(worker)
    m = client.get("/api/v1/materials", headers=H).json()["materials"][0]
    assert m["status"] == "failed" and m["questionCount"] == 0
    assert len(ai.calls) == 3 * m["chunksTotal"]


def test_delete_answers_review_grade_report(env):
    client, worker, _ = env
    material_id = upload(client, [("a.pptx", make_pptx(SLIDES), PPTX)]).json()["materials"][0]["id"]
    drain(worker)
    qs = client.get("/api/v1/questions", headers=H).json()["questions"]
    ox = next(q for q in qs if q["type"] == "ox")
    short = next(q for q in qs if q["type"] == "short")

    answers = [
        {"questionId": ox["id"], "result": "wrong", "answeredAt": "2026-11-08T10:00:00+09:00"},
        {"questionId": ox["id"], "result": "wrong", "answeredAt": "2026-11-08T11:00:00+09:00"},
        {"questionId": "q_none", "result": "wrong", "answeredAt": "2026-11-08T11:00:00+09:00"},
    ]
    assert client.post("/api/v1/answers", json={"answers": answers}, headers=H).json() == {"recorded": 2}
    item = client.get("/api/v1/review", headers=H).json()["items"][0]
    assert item["question"]["id"] == ox["id"] and item["wrongCount"] == 2 and item["lastWrongAt"].startswith("2026-11-08T11")

    r = client.post(f"/api/v1/questions/{short['id']}/grade", json={"userAnswer": "정 답"}, headers=H)
    assert r.json() == {"correct": True, "by": "rule"}
    r = client.post(f"/api/v1/questions/{short['id']}/grade", json={"userAnswer": "오답"}, headers=H)
    assert r.json() == {"correct": False, "by": "rule"}
    assert client.post(f"/api/v1/questions/{ox['id']}/grade", json={"userAnswer": "O"}, headers=H).status_code == 400
    assert client.post(f"/api/v1/questions/{ox['id']}/report", json={"reason": "wrong_answer"}, headers=H).status_code == 204

    assert client.delete(f"/api/v1/materials/{material_id}", headers=H).status_code == 204
    assert client.get("/api/v1/questions", headers=H).json()["questions"] == []
    # 자료와 함께 풀이 결과·신고도 지워짐
    db = client.app.state.db
    assert db.conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
    assert client.delete(f"/api/v1/materials/{material_id}", headers=H).json()["error"]["code"] == "not_found"


def test_abuse_limits(env, monkeypatch):
    client, worker, _ = env
    from app import main
    data = make_pptx(SLIDES)

    # 기기 ID를 바꿔도 같은 IP의 하루 업로드 한도에 걸림
    monkeypatch.setattr(main, "IP_DAILY_UPLOADS", 2)
    ip = {"Fly-Client-IP": "203.0.113.7"}
    assert upload(client, [("a.pptx", data, PPTX)], headers={**ip, "X-Device-Id": "a"}).status_code == 202
    assert upload(client, [("b.pptx", data, PPTX)], headers={**ip, "X-Device-Id": "b"}).status_code == 202
    r = upload(client, [("c.pptx", data, PPTX)], headers={**ip, "X-Device-Id": "c"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    drain(worker)


def test_server_daily_chunk_limit(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "DAILY_CHUNKS", 3)
    app = create_app(str(tmp_path / "t.sqlite3"), "http://ai/internal/v1", run_worker=False)
    with TestClient(app) as client:
        data = make_pptx(SLIDES)  # 조각 2개
        assert upload(client, [("a.pptx", data, PPTX)], headers={"X-Device-Id": "a"}).status_code == 202
        r = upload(client, [("b.pptx", data, PPTX)], headers={"X-Device-Id": "b"})
        assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
        # 거절된 요청은 자료도 사용량도 남기지 않음
        assert client.get("/api/v1/materials", headers={"X-Device-Id": "b"}).json()["materials"] == []


def test_input_size_limits(env, monkeypatch):
    client, _, _ = env
    from app import extract
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert upload(client, [("a.pptx", make_pptx(SLIDES), PPTX)], headers={"X-Device-Id": "x" * 65}).status_code == 400
    r = client.post("/api/v1/materials", headers={**H, "Content-Length": str(200 * 1024 * 1024)}, content=b"x")
    assert r.status_code == 413
    answers = [{"questionId": "q", "result": "wrong", "answeredAt": "t"}] * 201
    assert client.post("/api/v1/answers", json={"answers": answers}, headers=H).status_code == 400
    monkeypatch.setattr(extract, "MAX_UNZIPPED_BYTES", 1000)
    assert upload(client, [("bomb.pptx", make_pptx(SLIDES), PPTX)]).status_code == 415
