# WOOWANGTHON_TEAM_23_BE

FE가 호출하는 API 서버. 자료를 받아 쪽(슬라이드)별 글자를 뽑고, 약 2000자 조각마다 AI 서비스를 불러 문제(OX 3개, 단답 2개)를 만들어 저장함. 계약은 DOCS 레포 `api.md`를 따름.

## 실행 방법

Python 3.12, 패키지는 uv로 관리함.

```bash
uv sync
AI_URL=http://localhost:8001/internal/v1 uv run uvicorn --factory app.main:create_app --port 8080
```

- `AI_URL`: AI 서비스 주소. 기본값은 위와 같음. AI는 `OPENAI_API_KEY`, `OPENAI_MODEL`이 있어야 호출됨(AI 레포 README)
- `BE_DB_PATH`: SQLite 파일 위치. 기본값 `data/be.sqlite3`

테스트는 AI 서비스 없이 돎 (AI 응답을 테스트 안에서 정해 둠).

```bash
uv run pytest
```

## 배포 (Fly.io)

BE와 AI 서비스를 한 컨테이너에서 프로세스 2개로 띄움. 외부에는 BE(8080)만 열리고 AI(8001)는 컨테이너 안에서만 접근함. 빌드 때 AI 레포 `main`을 받아 옴(`Dockerfile`의 `AI_REF`).

- 주소: `https://scrollmate-be.fly.dev` (앱 `scrollmate-be`, 도쿄, 1대 512MB)
- SQLite는 볼륨 `be_data`(`/data`)에 저장함. 볼륨을 지우면 자료와 문제도 사라짐
- 모델은 `fly.toml`의 `OPENAI_MODEL`. 키는 저장소에 두지 않고 Fly 시크릿으로만 넣음

```bash
fly secrets set OPENAI_API_KEY=...
fly deploy --no-cache
curl https://scrollmate-be.fly.dev/api/v1/health
```

- 자동 배포는 쓰지 않음. 배포는 직접 `fly deploy --no-cache`로 함. 캐시를 쓰면 AI 레포를 받는 단계가 재사용되어 AI의 새 커밋이 반영되지 않음(2026-11-13 확인)
- 업로드한 자료의 글자는 OpenAI API로 전송됨

## 구조

- `app/main.py`: FE ↔ BE 엔드포인트, 파일·횟수 제한. 단답 채점은 AI를 부르지 않고 정답·허용 답안과 글자로만 비교함 (공백·대소문자 무시)
- `app/extract.py`: PDF(pypdf)·PPTX(python-pptx) 글자 추출, 조각 나누기, 근거 문장으로 쪽수 찾기
- `app/worker.py`: 문제 생성 작업자 1개. 조각 번호 순으로 골라 자료마다 번갈아 처리함. 실패한 조각은 3번까지 다시 시도함. 처음 올린 조각을 다 처리한 뒤 새 문제 채우기를 처리함

## 새 문제 채우기

- 앱이 `POST /answers`로 풀이 기록을 보내면, 그 자료에서 한 번도 안 푼 문제가 5개 미만일 때 새 문제를 만듦
- 자료의 조각을 돌아가며 고르고, 그 조각에서 이미 낸 문제를 AI에 `avoid`로 넘겨 같은 내용을 피하게 함
- 받은 뒤에도 유형·근거 문장·정답이 같거나 문제 문장이 85% 이상 같으면 버림
- 자료당 최대 10번, 한 번에 1건만 예약. 서버 전체 하루 생성 조각 한도에 포함됨
- 같은 풀이 기록(문제와 시각이 같음)은 한 번만 저장함. 앱이 기록을 보내지 않으면 채우지 않음
- `app/db.py`: SQLite 스키마

## 제한 (api.md 확정 사항)

| 항목 | 값 | 넘으면 |
|---|---|---|
| 한 번에 올리는 파일 | 1~5개 | `400 invalid_request` |
| 파일 크기, 쪽수 | 20MB, 100쪽(슬라이드) | `413 file_too_large` |
| 기기별 동시 처리 자료 | 5개 | `429 too_many_materials` |
| 기기별 하루 업로드 | 10개 | `429 rate_limited` |
| 전체 대기 조각 | 900개 | `429 rate_limited` |
| IP별 하루 업로드 (`Fly-Client-IP`, 배포 환경만) | 30개 (`BE_IP_DAILY_UPLOADS`) | `429 rate_limited` |
| 서버 전체 하루 생성 조각 | 2000개 (`BE_DAILY_CHUNKS`) | `429 rate_limited` |
| 요청 본문 | 101MB | `413 file_too_large` |
| PPTX 압축 해제 크기 | 200MB | `415 unsupported_file` |
| `X-Device-Id` 길이, 풀이 기록 한 번에 | 64자, 200개 | `400 invalid_request` |

로그인이 없어 기기 ID를 바꾸면 기기별 한도를 피할 수 있음. IP와 서버 전체 한도는 OpenAI 비용을 묶기 위함. 서버 전체 한도 2000조각은 평가 기준 약 $4. API 문서 화면(`/docs`)은 끔

## 아직 없는 것

- 구글 로그인 (BE #5). 지금은 `X-Device-Id`로만 구분함
- 정답 검증 호출(AI `verify`)은 생성 흐름에 넣지 않음

## 만들지 않기로 한 것 (11/13)

- 오답노트(`GET /review`), 시청 기록 집계와 조언(`POST /usage/daily`, `GET /advice/today`). 앱이 쓰지 않아 `GET /review`도 지움
- 감지 대상은 안드로이드 유튜브 쇼츠만. 인스타그램 릴스는 다루지 않음
