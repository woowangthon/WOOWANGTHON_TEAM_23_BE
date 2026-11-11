#!/bin/bash
# 한 프로세스가 죽으면 컨테이너를 내려 Fly가 다시 띄우게 함
(cd /ai && exec .venv/bin/uvicorn app.service:app --host 127.0.0.1 --port 8001) &
.venv/bin/uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8080 &
wait -n
exit 1
