"""
Jobs API — generic status endpoint for any Celery-backed long-running task.

Authorisation: a job can only be inspected by its owner (user_id stored in
the Redis record).  This prevents data leakage between tenants on a shared
Redis instance.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.auth.dependencies import get_current_active_user
from app.schemas.response import ResponseModel
from app.services import job_service

router = APIRouter()


@router.get("/{job_id}", response_model=ResponseModel)
async def get_job_status(
    job_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Return live status for a Celery job (upload or export)."""
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    if job.get("user_id") != str(current_user["_id"]):
        # Hide existence from non-owners.
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return ResponseModel(success=True, message="Job status", data=job)
