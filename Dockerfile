FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# AI 서비스는 별도 레포라 빌드 때 받아 와서 같은 컨테이너에서 띄움
ARG AI_REF=main
RUN git clone --depth 1 --branch ${AI_REF} https://github.com/woowangthon/WOOWANGTHON_TEAM_23_AI.git /ai \
    && cd /ai && uv sync --frozen --no-dev

WORKDIR /be
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY start.sh ./

ENV BE_DB_PATH=/data/be.sqlite3 AI_URL=http://127.0.0.1:8001/internal/v1
CMD ["bash", "start.sh"]
