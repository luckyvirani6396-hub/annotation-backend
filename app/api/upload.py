"""
Upload API -- dataset ingestion endpoints.

POST /api/upload/
    Stream image files (any quantity) straight to a staging directory,
    queue a Celery worker, and return a job_id immediately.
    An optional COCO JSON annotation file makes the dataset annotated from the start.

POST /api/upload/zip
    Stream a single ZIP archive (up to 50 GB) to staging and queue a
    progressive extraction job.  The dataset becomes queryable in the
    annotation studio after the first batch (~500 images) is inserted --
    usually within seconds of the upload completing.
"""

import json
import os
import shutil
import uuid
from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, Form, Depends
from loguru import logger

from app.auth.dependencies import get_current_active_user
from app.config.settings import settings
from app.services import job_service
from app.schemas.response import ResponseModel
from app.schemas.annotation import COCOJSON

router = APIRouter()

_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
_ZIP_MAX_BYTES = 50 * 1024 ** 3  # 50 GB hard cap


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

async def _check_append(mode: str, dataset_name: str, user_id) -> None:
    """Raise 404 when mode is 'append' and the dataset does not exist."""
    if mode == "append":
        from app.config.database import get_collection
        existing = await get_collection("dataset_metadata").find_one(
            {"dataset_name": dataset_name, "user_id": user_id, "is_active": True}
        )
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"Cannot append: dataset '{dataset_name}' not found for this user",
            )


# ---------------------------------------------------------------------------
# POST /api/upload/  -- image files (any quantity)
# ---------------------------------------------------------------------------

@router.post("/")
async def upload_images(
    images: List[UploadFile] = File(..., description="Image files (1 - 100 000+)"),
    dataset_name: str = Form(..., description="Unique dataset identifier"),
    annotation_file: Optional[UploadFile] = File(
        None, description="Optional COCO JSON annotation"
    ),
    annotation_format: Optional[str] = Form(None, description="'coco' or 'yolo'"),
    yolo_labels_zip: Optional[UploadFile] = File(None, description="Optional YOLO labels.zip"),
    yolo_data_yaml: Optional[UploadFile] = File(None, description="Optional YOLO data.yaml"),
    mode: str = Form("create", description="'create' | 'append'"),
    current_user: dict = Depends(get_current_active_user),
):
    """Stream image files to staging and enqueue a Celery ingest job.

    Returns {job_id} immediately.  Poll GET /api/jobs/{job_id} to watch
    progress; redirect to /annotate/{dataset_name} once status == succeeded.
    """
    from app.tasks.upload_tasks import process_upload_job

    user_id = current_user["_id"]
    if mode not in ("create", "append"):
        raise HTTPException(status_code=400, detail="mode must be 'create' or 'append'")
    await _check_append(mode, dataset_name, user_id)

    job_id = uuid.uuid4().hex
    staging_dir = os.path.join(settings.STAGING_DIR, job_id)
    os.makedirs(staging_dir, exist_ok=True)

    manifest: List[dict] = []
    skipped: List[str] = []

    try:
        for file in images:
            if not file.filename:
                continue
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in _ALLOWED_EXTS:
                skipped.append(f"{file.filename} -- unsupported extension '{ext}'")
                continue
            stored = f"{uuid.uuid4().hex}{ext}"
            dst = os.path.join(staging_dir, stored)
            # Stream in 1-MiB chunks -- never spikes memory regardless of batch size.
            with open(dst, "wb") as fh:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            manifest.append({"original": file.filename, "stored": stored})

        if annotation_format == "coco" and annotation_file is not None:
            ann_path = os.path.join(staging_dir, "annotation.json")
            with open(ann_path, "wb") as fh:
                while True:
                    chunk = await annotation_file.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            # Validate upfront so the worker does not fail minutes later.
            try:
                with open(ann_path, "r", encoding="utf-8") as fh:
                    coco_raw = json.load(fh)
                COCOJSON(**coco_raw)
            except Exception as exc:
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise HTTPException(status_code=400, detail=f"Invalid COCO JSON: {exc}")
                
        elif annotation_format == "yolo" and yolo_labels_zip is not None and yolo_data_yaml is not None:
            labels_path = os.path.join(staging_dir, "yolo_labels.zip")
            with open(labels_path, "wb") as fh:
                while True:
                    chunk = await yolo_labels_zip.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            yaml_path = os.path.join(staging_dir, "yolo_data.yaml")
            with open(yaml_path, "wb") as fh:
                while True:
                    chunk = await yolo_data_yaml.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)

        # Manifest enables a clean restart if the worker crashes mid-job.
        manifest_path = os.path.join(staging_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)

    except HTTPException:
        raise
    except Exception as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        logger.error(f"[upload] staging failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Staging failed: {exc}")

    if not manifest:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="No valid images provided")

    # Create the tracker BEFORE dispatching so /api/jobs/{job_id} responds
    # immediately even when the broker is briefly slow.
    job_service.create_job(
        job_type="upload",
        user_id=user_id,
        dataset_name=dataset_name,
        total=len(manifest),
        extra={"skipped": skipped[:50]},
        job_id=job_id,
    )
    process_upload_job.apply_async(
        kwargs={
            "job_id": job_id,
            "user_id": str(user_id),
            "dataset_name": dataset_name,
            "staging_dir": staging_dir,
            "mode": mode,
            "annotation_format": annotation_format,
        },
        task_id=job_id,
    )

    logger.info(
        f"[upload] job={job_id} dataset={dataset_name} "
        f"queued {len(manifest)} files, skipped {len(skipped)}"
    )
    return ResponseModel(
        success=True,
        message="Upload accepted -- processing in background",
        data={
            "job_id": job_id,
            "dataset_name": dataset_name,
            "queued": len(manifest),
            "skipped": skipped,
            "status_url": f"/api/jobs/{job_id}",
        },
    )


# ---------------------------------------------------------------------------
# POST /api/upload/zip  -- ZIP archive (up to 50 GB)
# ---------------------------------------------------------------------------

@router.post("/zip")
async def upload_zip(
    zip_file: UploadFile = File(..., description="ZIP archive containing images (up to 50 GB)"),
    dataset_name: str = Form(..., description="Unique dataset identifier"),
    annotation_file: Optional[UploadFile] = File(
        None, description="Optional COCO JSON annotation"
    ),
    annotation_format: Optional[str] = Form(None, description="'coco' or 'yolo'"),
    yolo_labels_zip: Optional[UploadFile] = File(None, description="Optional YOLO labels.zip"),
    yolo_data_yaml: Optional[UploadFile] = File(None, description="Optional YOLO data.yaml"),
    mode: str = Form("create", description="'create' | 'append'"),
    current_user: dict = Depends(get_current_active_user),
):
    """Stream a ZIP archive to staging and queue a progressive extraction job.

    The ZIP may contain _annotations.json (COCO format) at its root, which
    is discovered and applied automatically after all images are extracted.
    An external annotation file can also be provided via annotation_file.

    Returns {job_id} immediately.  The dataset is queryable in the annotation
    studio after the first batch (~500 images) is inserted.
    """
    from app.tasks.upload_tasks import process_zip_upload_job

    user_id = current_user["_id"]
    if mode not in ("create", "append"):
        raise HTTPException(status_code=400, detail="mode must be 'create' or 'append'")
    await _check_append(mode, dataset_name, user_id)

    if not (zip_file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a .zip archive")

    job_id = uuid.uuid4().hex
    staging_dir = os.path.join(settings.STAGING_DIR, job_id)
    os.makedirs(staging_dir, exist_ok=True)

    try:
        # Stream ZIP in 4-MiB chunks -- never load the full archive into memory.
        zip_staging_path = os.path.join(staging_dir, "upload.zip")
        total_bytes = 0
        with open(zip_staging_path, "wb") as fh:
            while True:
                chunk = await zip_file.read(4 * 1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > _ZIP_MAX_BYTES:
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    raise HTTPException(status_code=413, detail="ZIP exceeds the 50 GB limit")
                fh.write(chunk)

        annotation_filename: Optional[str] = None
        if annotation_format == "coco" and annotation_file is not None:
            ann_path = os.path.join(staging_dir, "annotation.json")
            with open(ann_path, "wb") as fh:
                while True:
                    chunk = await annotation_file.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            try:
                with open(ann_path, "r", encoding="utf-8") as fh:
                    coco_raw = json.load(fh)
                COCOJSON(**coco_raw)
            except Exception as exc:
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise HTTPException(status_code=400, detail=f"Invalid COCO JSON: {exc}")
            annotation_filename = "annotation.json"
        
        elif annotation_format == "yolo" and yolo_labels_zip is not None and yolo_data_yaml is not None:
            labels_path = os.path.join(staging_dir, "yolo_labels.zip")
            with open(labels_path, "wb") as fh:
                while True:
                    chunk = await yolo_labels_zip.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            yaml_path = os.path.join(staging_dir, "yolo_data.yaml")
            with open(yaml_path, "wb") as fh:
                while True:
                    chunk = await yolo_data_yaml.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            annotation_filename = "yolo" # Using this to indicate YOLO logic is needed

    except HTTPException:
        raise
    except Exception as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        logger.error(f"[upload/zip] staging failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Staging failed: {exc}")

    job_service.create_job(
        job_type="upload",
        user_id=user_id,
        dataset_name=dataset_name,
        total=0,  # Actual total set by the worker after scanning the ZIP
        extra={"source": "zip", "zip_bytes": total_bytes},
        job_id=job_id,
    )
    process_zip_upload_job.apply_async(
        kwargs={
            "job_id": job_id,
            "user_id": str(user_id),
            "dataset_name": dataset_name,
            "staging_dir": staging_dir,
            "zip_filename": "upload.zip",
            "annotation_filename": annotation_filename,
            "mode": mode,
            "annotation_format": annotation_format,
        },
        task_id=job_id,
    )

    logger.info(
        f"[upload/zip] job={job_id} dataset={dataset_name} "
        f"zip={total_bytes:,} bytes queued"
    )
    return ResponseModel(
        success=True,
        message="ZIP upload accepted -- extraction running in background",
        data={
            "job_id": job_id,
            "dataset_name": dataset_name,
            "zip_size_bytes": total_bytes,
            "status_url": f"/api/jobs/{job_id}",
        },
    )

