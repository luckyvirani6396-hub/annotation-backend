"""
Job tracking service — Redis-backed progress tracker for long-running tasks.

Why Redis (and not Mongo)?  Job state is hot, ephemeral and updated thousands
of times per minute during a 100 k image ingest.  Redis hash writes are O(1)
and never need an index; Mongo would buckle under that write rate.

State schema (Redis HASH at key `job:<job_id>`):
    id            <uuid>
    type          upload | export
    status        pending | running | succeeded | failed
    progress      0-100 (int)
    processed     processed item count
    total         total item count
    dataset_name  target dataset
    user_id       owner
    error         last error message (only set when status=failed)
    result_url    download URL (only set for export jobs)
    created_at    ISO-8601
    updated_at    ISO-8601
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import redis

from app.config.settings import settings


# ---------------------------------------------------------------------------
# Redis client (sync; safe to share between async FastAPI and sync workers)
# ---------------------------------------------------------------------------
_redis: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    """Return a process-wide Redis client."""
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_keepalive=True,
        )
    return _redis


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------
def _key(job_id: str) -> str:
    return f"job:{job_id}"


def create_job(
    job_type: str,
    user_id: str,
    dataset_name: str,
    total: int = 0,
    extra: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
) -> str:
    """Create a job record and return its ID.

    Pass ``job_id`` to align the Redis key with an externally generated
    Celery ``task_id`` so a single identifier tracks the work end-to-end.
    """
    job_id = job_id or uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    payload: Dict[str, Any] = {
        "id": job_id,
        "type": job_type,
        "status": "pending",
        "progress": 0,
        "processed": 0,
        "total": int(total),
        "dataset_name": dataset_name,
        "user_id": str(user_id),
        "created_at": now,
        "updated_at": now,
    }
    if extra:
        payload["extra"] = json.dumps(extra)

    r = get_redis()
    r.hset(_key(job_id), mapping={k: str(v) for k, v in payload.items()})
    r.expire(_key(job_id), settings.JOB_TTL_SECONDS)
    return job_id


def update_job(job_id: str, **fields: Any) -> None:
    """Update arbitrary fields on a job. Sets `updated_at` automatically."""
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().isoformat()
    r = get_redis()
    r.hset(_key(job_id), mapping={k: str(v) for k, v in fields.items()})


def increment_processed(job_id: str, n: int = 1, total: Optional[int] = None) -> None:
    """Atomically bump `processed` and recompute `progress`."""
    r = get_redis()
    pipe = r.pipeline()
    pipe.hincrby(_key(job_id), "processed", n)
    pipe.hget(_key(job_id), "total")
    new_processed, total_str = pipe.execute()
    try:
        denom = int(total if total is not None else (total_str or 1)) or 1
        progress = min(100, int((int(new_processed) * 100) / denom))
    except (TypeError, ValueError):
        progress = 0
    r.hset(_key(job_id), mapping={
        "progress": str(progress),
        "updated_at": datetime.utcnow().isoformat(),
    })


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Return the full job record, or None if not found / expired."""
    r = get_redis()
    data = r.hgetall(_key(job_id))
    if not data:
        return None
    # Coerce numeric fields back to ints for the API consumer.
    for key in ("progress", "processed", "total"):
        if key in data:
            try:
                data[key] = int(data[key])
            except ValueError:
                data[key] = 0
    if "extra" in data:
        try:
            data["extra"] = json.loads(data["extra"])
        except (TypeError, json.JSONDecodeError):
            pass
    return data


def mark_running(job_id: str) -> None:
    update_job(job_id, status="running")


def mark_succeeded(job_id: str, **extra: Any) -> None:
    update_job(job_id, status="succeeded", progress=100, **extra)


def mark_failed(job_id: str, error: str) -> None:
    update_job(job_id, status="failed", error=str(error)[:500])
