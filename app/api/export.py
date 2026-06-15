"""
Export API — Roboflow-style dataset download endpoints.

Default export (format=coco) always produces a ZIP bundle:

    {dataset_name}.zip
    ├── images/                          ← all dataset images
    ├── annotations/
    │   └── instances_default.json       ← COCO JSON annotations
    └── README.md

The heavy ZIP assembly (parallel file reads + compression) runs inside a
ThreadPoolExecutor via ``run_in_executor`` so the FastAPI event loop is
never blocked, even for datasets with tens of thousands of images.
"""

import asyncio
import io
import os
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger

from app.auth.dependencies import get_current_active_user
from app.config.database import db_manager
from app.repositories.export_history_repository import ExportHistoryRepository
from app.repositories.rbac_repository import TaskBatchRepository
from app.services.annotation_service import AnnotationService
from app.services.export_service import ExportService
from app.services import job_service
from app.config.settings import settings

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_export_target(
    dataset_name: str,
    user_id: str,
    batch_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], str, str, Optional[Dict[str, Any]]]:
    """Load dataset payload and derive export filename prefix.

    Returns (dataset, owner_user_id, filename_prefix, batch_doc).
    """
    batch_doc = None
    image_id_min = None
    image_id_max = None
    filename_prefix = dataset_name

    if batch_id:
        batch_repo = TaskBatchRepository(db_manager.get_db())
        batch_doc = await batch_repo.get_batch_by_id(batch_id)
        if not batch_doc:
            raise HTTPException(status_code=404, detail="Batch not found")
        batch_dataset = batch_doc.get("project_id")
        if batch_dataset != dataset_name:
            raise HTTPException(
                status_code=400,
                detail="Batch does not belong to the selected dataset",
            )
        image_id_min = batch_doc.get("start_index")
        image_id_max = batch_doc.get("end_index")
        filename_prefix = f"{dataset_name}_batch{batch_doc.get('batch_number', '')}"

    owner_id = await AnnotationService.get_dataset_owner_id(dataset_name)
    if not owner_id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    load_user_id = owner_id

    dataset = await AnnotationService.get_annotation_dataset(
        dataset_name,
        load_user_id,
        image_id_min=image_id_min,
        image_id_max=image_id_max,
    )
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    return dataset, load_user_id, filename_prefix, batch_doc


async def _log_export(
    *,
    user_id: str,
    scope: str,
    dataset_name: str,
    fmt: str,
    status: str,
    filename: str,
    image_count: int,
    split_enabled: bool,
    batch_id: Optional[str] = None,
    batch_number: Optional[int] = None,
    job_id: Optional[str] = None,
    file_size: Optional[int] = None,
    error: Optional[str] = None,
    history_id: Optional[str] = None,
) -> str:
    repo = ExportHistoryRepository(db_manager.get_db())
    now = datetime.utcnow()
    payload = {
        "user_id": str(user_id),
        "scope": scope,
        "dataset_name": dataset_name,
        "batch_id": batch_id,
        "batch_number": batch_number,
        "format": fmt,
        "status": status,
        "filename": filename,
        "image_count": image_count,
        "split_enabled": split_enabled,
        "job_id": job_id,
        "file_size": file_size,
        "error": error,
        "completed_at": now if status in ("completed", "failed") else None,
    }
    if history_id:
        await repo.update_record(history_id, **payload)
        return history_id
    return await repo.create_record(payload)


# ---------------------------------------------------------------------------
# Export history
# ---------------------------------------------------------------------------

@router.get("/history")
async def list_export_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    dataset_name: Optional[str] = Query(None),
    scope: Optional[str] = Query(None, description="dataset or batch"),
    search: Optional[str] = Query(None, description="Search dataset, filename, or format"),
    date_from: Optional[str] = Query(None, description="ISO date YYYY-MM-DD (inclusive)"),
    date_to: Optional[str] = Query(None, description="ISO date YYYY-MM-DD (inclusive)"),
    current_user: dict = Depends(get_current_active_user),
):
    """Return paginated export history for the current user."""
    from datetime import datetime as dt, time as dt_time

    parsed_from = None
    parsed_to = None
    try:
        if date_from:
            parsed_from = dt.combine(dt.strptime(date_from, "%Y-%m-%d").date(), dt_time.min)
        if date_to:
            parsed_to = dt.combine(dt.strptime(date_to, "%Y-%m-%d").date(), dt_time.max)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    repo = ExportHistoryRepository(db_manager.get_db())
    rows, total = await repo.list_for_user(
        user_id=str(current_user["_id"]),
        page=page,
        page_size=page_size,
        dataset_name=dataset_name,
        scope=scope,
        search=search,
        date_from=parsed_from,
        date_to=parsed_to,
    )
    total_pages = (total + page_size - 1) // page_size if total else 0
    return {
        "success": True,
        "data": {
            "items": [repo.serialize(r) for r in rows],
            "pagination": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            },
        },
    }


@router.get("/history/{history_id}/download")
async def download_export_history_item(
    history_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Re-download a completed export from history (cached job file or fresh rebuild)."""
    from app.services.export_service import normalize_format

    repo = ExportHistoryRepository(db_manager.get_db())
    doc = await repo.get_by_id(history_id)
    if not doc or doc.get("user_id") != str(current_user["_id"]):
        raise HTTPException(status_code=404, detail="Export not found")

    if doc.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Only completed exports can be downloaded")

    zip_filename = doc.get("filename") or "export.zip"
    job_id = doc.get("job_id")
    if job_id:
        job = job_service.get_job(job_id)
        if job and job.get("user_id") == str(current_user["_id"]):
            zip_path = job.get("zip_path")
            if zip_path and os.path.isfile(zip_path):
                return FileResponse(
                    zip_path,
                    media_type="application/zip",
                    filename=os.path.basename(zip_path),
                    headers={
                        "Content-Disposition": f'attachment; filename="{os.path.basename(zip_path)}"',
                    },
                )

    dataset_name = doc.get("dataset_name")
    batch_id = doc.get("batch_id")
    try:
        canonical = normalize_format(doc.get("format") or "coco")
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    dataset, _load_user_id, filename_prefix, _batch_doc = await _resolve_export_target(
        dataset_name,
        current_user["_id"],
        batch_id=batch_id,
    )
    zip_filename = doc.get("filename") or f"{filename_prefix}_{canonical}.zip"

    loop = asyncio.get_event_loop()
    try:
        zip_buf: io.BytesIO = await loop.run_in_executor(
            None,
            ExportService.build_zip,
            dataset,
            filename_prefix,
            canonical,
            bool(doc.get("split_enabled")),
            0.7,
            0.15,
            0.15,
        )
    except ValueError as ve:
        raise HTTPException(status_code=500, detail=str(ve))
    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_filename}"'},
    )


# ---------------------------------------------------------------------------
# Async export — enqueue a build, poll status, then download
# ---------------------------------------------------------------------------

@router.post("/{dataset_name}/async")
async def export_dataset_async(
    dataset_name: str,
    format: str = "coco",
    batch_id: Optional[str] = Query(None, description="Export only images in this batch"),
    split_dataset: bool = False,
    train_split: float = 0.7,
    val_split: float = 0.15,
    test_split: float = 0.15,
    current_user: dict = Depends(get_current_active_user),
):
    """Enqueue a dataset-ZIP build in any supported format and return a job_id."""
    from app.tasks.export_tasks import build_dataset_zip
    from app.services.export_service import normalize_format

    try:
        canonical = normalize_format(format)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    if split_dataset:
        total = train_split + val_split + test_split
        if not (0.99 <= total <= 1.01):
            raise HTTPException(
                status_code=400,
                detail=f"Split percentages must sum to 1.0, got {total}",
            )

    user_id = current_user["_id"]
    dataset, load_user_id, filename_prefix, batch_doc = await _resolve_export_target(
        dataset_name, user_id, batch_id=batch_id,
    )

    import uuid as _uuid
    job_id = _uuid.uuid4().hex
    scope = "batch" if batch_id else "dataset"
    zip_filename = f"{filename_prefix}_{canonical}.zip"

    history_id = await _log_export(
        user_id=str(user_id),
        scope=scope,
        dataset_name=dataset_name,
        fmt=canonical,
        status="queued",
        filename=zip_filename,
        image_count=len(dataset["images"]),
        split_enabled=split_dataset,
        batch_id=batch_id,
        batch_number=batch_doc.get("batch_number") if batch_doc else None,
        job_id=job_id,
    )

    job_service.create_job(
        job_type="export",
        user_id=user_id,
        dataset_name=dataset_name,
        total=len(dataset["images"]),
        job_id=job_id,
        extra={
            "format": canonical,
            "batch_id": batch_id,
            "history_id": history_id,
            "filename_prefix": filename_prefix,
        },
    )
    build_dataset_zip.apply_async(
        kwargs={
            "job_id": job_id,
            "user_id": str(load_user_id),
            "dataset_name": dataset_name,
            "fmt": canonical,
            "split_dataset": split_dataset,
            "train_split": train_split,
            "val_split": val_split,
            "test_split": test_split,
            "batch_id": batch_id,
            "filename_prefix": filename_prefix,
            "history_id": history_id,
            "exporter_user_id": str(user_id),
        },
        task_id=job_id,
    )

    return {
        "success": True,
        "message": "Export job queued",
        "data": {
            "job_id": job_id,
            "history_id": history_id,
            "status_url": f"/api/jobs/{job_id}",
            "format": canonical,
            "scope": scope,
            "batch_id": batch_id,
            "split": split_dataset,
            "train": train_split,
            "val": val_split,
            "test": test_split,
        },
    }


@router.get("/download/{job_id}")
async def download_export(
    job_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Download the ZIP produced by a finished export job."""
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    if job.get("user_id") != str(current_user["_id"]):
        raise HTTPException(status_code=404, detail="Job not found or expired")
    if job.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail=f"Job not ready (status={job.get('status')})")

    zip_path = job.get("zip_path")
    if not zip_path or not os.path.isfile(zip_path):
        raise HTTPException(status_code=410, detail="Export artifact no longer available")

    filename = os.path.basename(zip_path)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Sync export (small/medium datasets)
# ---------------------------------------------------------------------------

@router.get("/{dataset_name}")
async def export_dataset(
    dataset_name: str,
    format: str = "coco",
    batch_id: Optional[str] = Query(None, description="Export only images in this batch"),
    split_dataset: bool = False,
    train_split: float = 0.7,
    val_split: float = 0.15,
    test_split: float = 0.15,
    current_user: dict = Depends(get_current_active_user),
):
    """Export a dataset (or batch slice) as a self-contained ZIP bundle."""
    from app.services.export_service import normalize_format, SUPPORTED_FORMATS

    history_id = None
    try:
        user_id = current_user["_id"]
        try:
            canonical = normalize_format(format)
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))

        if split_dataset:
            total = train_split + val_split + test_split
            if not (0.99 <= total <= 1.01):
                raise HTTPException(
                    status_code=400,
                    detail=f"Split percentages must sum to 1.0, got {total}",
                )

        dataset, _load_user_id, filename_prefix, batch_doc = await _resolve_export_target(
            dataset_name, user_id, batch_id=batch_id,
        )
        scope = "batch" if batch_id else "dataset"
        zip_filename = f"{filename_prefix}_{canonical}.zip"

        history_id = await _log_export(
            user_id=str(user_id),
            scope=scope,
            dataset_name=dataset_name,
            fmt=canonical,
            status="running",
            filename=zip_filename,
            image_count=len(dataset["images"]),
            split_enabled=split_dataset,
            batch_id=batch_id,
            batch_number=batch_doc.get("batch_number") if batch_doc else None,
        )

        loop = asyncio.get_event_loop()
        try:
            zip_buf: io.BytesIO = await loop.run_in_executor(
                None,
                ExportService.build_zip,
                dataset,
                filename_prefix,
                canonical,
                split_dataset,
                train_split,
                val_split,
                test_split,
            )
        except ValueError as ve:
            if history_id:
                await _log_export(
                    user_id=str(user_id),
                    scope=scope,
                    dataset_name=dataset_name,
                    fmt=canonical,
                    status="failed",
                    filename=zip_filename,
                    image_count=len(dataset["images"]),
                    split_enabled=split_dataset,
                    batch_id=batch_id,
                    batch_number=batch_doc.get("batch_number") if batch_doc else None,
                    error=str(ve)[:500],
                    history_id=history_id,
                )
            raise HTTPException(status_code=500, detail=str(ve))
        zip_bytes = zip_buf.getvalue()
        await _log_export(
            user_id=str(user_id),
            scope=scope,
            dataset_name=dataset_name,
            fmt=canonical,
            status="completed",
            filename=zip_filename,
            image_count=len(dataset["images"]),
            split_enabled=split_dataset,
            batch_id=batch_id,
            batch_number=batch_doc.get("batch_number") if batch_doc else None,
            file_size=len(zip_bytes),
            history_id=history_id,
        )
        zip_buf.seek(0)
        return StreamingResponse(
            zip_buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_filename}"',
                "X-Export-Format": canonical,
                "X-Export-Formats-Supported": ",".join(SUPPORTED_FORMATS),
                "X-Export-Scope": scope,
            },
        )

    except HTTPException:
        if history_id:
            await _log_export(
                user_id=str(current_user["_id"]),
                scope="batch" if batch_id else "dataset",
                dataset_name=dataset_name,
                fmt=format,
                status="failed",
                filename="",
                image_count=0,
                split_enabled=split_dataset,
                batch_id=batch_id,
                error="Export failed",
                history_id=history_id,
            )
        raise
    except Exception as exc:
        logger.error(f"[export] '{dataset_name}' failed: {exc}")
        if history_id:
            await _log_export(
                user_id=str(current_user["_id"]),
                scope="batch" if batch_id else "dataset",
                dataset_name=dataset_name,
                fmt=format,
                status="failed",
                filename="",
                image_count=0,
                split_enabled=split_dataset,
                batch_id=batch_id,
                error=str(exc)[:500],
                history_id=history_id,
            )
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/_meta/formats")
async def list_formats(
    current_user: dict = Depends(get_current_active_user),
):
    """Return the list of supported export formats."""
    from app.services.export_service import SUPPORTED_FORMATS
    return {
        "success": True,
        "data": {
            "formats": [
                {
                    "id": "coco",
                    "label": "COCO JSON",
                    "extension": "zip",
                    "description": "COCO 2017 — images + instances_default.json + README",
                },
                {
                    "id": "yolov8",
                    "label": "YOLOv8",
                    "extension": "zip",
                    "description": "Ultralytics YOLOv8 — images/, labels/, data.yaml",
                },
                {
                    "id": "pascal_voc",
                    "label": "Pascal VOC",
                    "extension": "zip",
                    "description": "Pascal VOC 2012 — JPEGImages/, Annotations/*.xml",
                },
                {
                    "id": "json",
                    "label": "ANO Studio JSON",
                    "extension": "zip",
                    "description": "Lossless native JSON — images + annotations.json",
                },
            ],
            "default": "coco",
            "all": list(SUPPORTED_FORMATS),
        },
    }
