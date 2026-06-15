#!/bin/sh
set -e
exec celery -A app.tasks.celery_app.celery_app worker \
  --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
  --concurrency="${CELERY_WORKER_CONCURRENCY:-4}" \
  -Q uploads,exports,annostudio
