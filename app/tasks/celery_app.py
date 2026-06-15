"""
Celery application — distributed task queue for image processing & export.

Run a worker locally:
    celery -A app.tasks.celery_app.celery_app worker --loglevel=INFO --concurrency=8

Run a beat scheduler (not required today, reserved for future jobs):
    celery -A app.tasks.celery_app.celery_app beat --loglevel=INFO

Monitoring UI (Flower):
    celery -A app.tasks.celery_app.celery_app flower --port=5555
"""

from __future__ import annotations

from celery import Celery

from app.config.settings import settings

celery_app = Celery(
    "annostudio",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.upload_tasks",
        "app.tasks.export_tasks",
    ],
)

celery_app.conf.update(
    task_default_queue=settings.CELERY_TASK_DEFAULT_QUEUE,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Acks late + reject-on-worker-lost give at-least-once delivery so an
    # interrupted worker doesn't lose a 100 k-image batch.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # One task at a time per worker process — heavy I/O tasks don't benefit
    # from prefetch and over-prefetch starves other workers.
    worker_prefetch_multiplier=settings.CELERY_WORKER_PREFETCH,
    worker_max_tasks_per_child=200,         # recycle workers to bound RSS
    broker_connection_retry_on_startup=True,
    result_expires=settings.JOB_TTL_SECONDS,
    task_time_limit=60 * 60 * 6,            # hard kill at 6 h
    task_soft_time_limit=60 * 60 * 5,       # graceful at 5 h
    task_track_started=True,
)

# Route long-running tasks to a dedicated queue if scaling out later.
celery_app.conf.task_routes = {
    "app.tasks.upload_tasks.*":  {"queue": "uploads"},
    "app.tasks.export_tasks.*":  {"queue": "exports"},
}

# Explicitly register the ZIP task so Flower and the beat scheduler can see it.
celery_app.conf.task_annotations = {
    "upload.process_zip_upload_job": {"rate_limit": "10/m"},
}
