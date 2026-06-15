#!/bin/sh
# Single-container mode for Railway / Render (shared local disk for staging + uploads).
set -e

celery -A app.tasks.celery_app.celery_app worker \
  --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
  --concurrency="${CELERY_WORKER_CONCURRENCY:-2}" \
  -Q uploads,exports,annostudio &

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-9000}" --workers "${UVICORN_WORKERS:-2}"
