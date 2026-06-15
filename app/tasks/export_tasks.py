"""
Async export pipeline — Celery task that builds Roboflow-style COCO ZIPs.

The synchronous endpoint at /api/export/{dataset} is still available for
small datasets.  This async path is preferable for large datasets because
it streams a download URL back to the client instead of holding the HTTP
connection open for minutes while the ZIP is built.

Output:
    {EXPORT_DIR}/{job_id}/{dataset_name}_coco.zip
exposed at:
    GET /api/export/download/{job_id}
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

from bson import ObjectId
from celery.utils.log import get_task_logger
from pymongo import MongoClient

from app.config.settings import settings
from app.services import job_service
from app.services.export_service import ExportService
from app.tasks.celery_app import celery_app

logger = get_task_logger(__name__)

_mongo_client: Optional[MongoClient] = None


def _db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(settings.MONGODB_URL)
    return _mongo_client[settings.DATABASE_NAME]


def _load_dataset(
    dataset_name: str,
    user_id: str,
    image_id_min: Optional[int] = None,
    image_id_max: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Load a full dataset using sync PyMongo (mirrors the async version)."""
    db = _db()
    meta = db["dataset_metadata"].find_one(
        {"dataset_name": dataset_name, "user_id": user_id, "is_active": True}
    )
    if not meta:
        return None
    dataset_id = meta["_id"]

    img_query: Dict[str, Any] = {"user_id": user_id, "dataset_id": dataset_id}
    ann_query: Dict[str, Any] = {"user_id": user_id, "dataset_id": dataset_id}
    if image_id_min is not None or image_id_max is not None:
        img_range: Dict[str, int] = {}
        if image_id_min is not None:
            img_range["$gte"] = image_id_min
        if image_id_max is not None:
            img_range["$lte"] = image_id_max
        img_query["image_id"] = img_range
        ann_query["image_id"] = img_range

    categories = list(db["categories"].find({"user_id": user_id, "dataset_id": dataset_id}))
    images = list(db["images"].find(img_query))
    annotations = list(db["annotations"].find(ann_query))

    return {
        "metadata": {**meta, "_id": str(meta["_id"])},
        "categories": [
            {"id": c["category_id"], "name": c["name"], "supercategory": c.get("supercategory", "")}
            for c in categories
        ],
        "images": [
            {
                "id": i["image_id"],
                "file_name": i["file_name"],
                "stored_filename": i.get("stored_filename"),
                "file_path": i.get("file_path"),
                "width": i["width"],
                "height": i["height"],
                "date_captured": i.get("date_captured", ""),
                "license": 1,
                "extra": i.get("extra", {}),
            }
            for i in images
        ],
        "annotations": [
            {
                "id": a["annotation_id"],
                "image_id": a["image_id"],
                "category_id": a["category_id"],
                "bbox": a["bbox"],
                "area": a["area"],
                "segmentation": a.get("segmentation", []),
                "iscrowd": a.get("iscrowd", 0),
            }
            for a in annotations
        ],
    }


def _resolve_owner_id(dataset_name: str) -> Optional[str]:
    db = _db()
    meta = db["dataset_metadata"].find_one({"dataset_name": dataset_name, "is_active": True})
    return str(meta["user_id"]) if meta else None


def _update_history(history_id: Optional[str], **fields: Any) -> None:
    if not history_id or not ObjectId.is_valid(history_id):
        return
    fields["updated_at"] = datetime.utcnow()
    if fields.get("status") in ("completed", "failed"):
        fields.setdefault("completed_at", datetime.utcnow())
    _db()["export_history"].update_one(
        {"_id": ObjectId(history_id)},
        {"$set": fields},
    )


@celery_app.task(name="export.build_dataset_zip", bind=True)
def build_dataset_zip(
    self,
    job_id: str,
    user_id: str,
    dataset_name: str,
    fmt: str = "coco",
    split_dataset: bool = False,
    train_split: float = 0.7,
    val_split: float = 0.15,
    test_split: float = 0.15,
    batch_id: Optional[str] = None,
    filename_prefix: Optional[str] = None,
    history_id: Optional[str] = None,
    exporter_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a dataset ZIP in ``fmt`` and write it to ``EXPORT_DIR``."""
    logger.info(
        f"[export-job {job_id}] start — dataset={dataset_name} fmt={fmt} "
        f"split={split_dataset} batch_id={batch_id}"
    )
    job_service.mark_running(job_id)
    _update_history(history_id, status="running")

    prefix = filename_prefix or dataset_name
    out_name = f"{prefix}_{fmt}.zip"

    try:
        image_id_min = None
        image_id_max = None
        if batch_id and ObjectId.is_valid(batch_id):
            batch = _db()["task_batches"].find_one({"_id": ObjectId(batch_id)})
            if batch:
                image_id_min = batch.get("start_index")
                image_id_max = batch.get("end_index")
                if not filename_prefix:
                    prefix = f"{dataset_name}_batch{batch.get('batch_number', '')}"
                    out_name = f"{prefix}_{fmt}.zip"

        load_user_id = user_id
        if batch_id:
            owner = _resolve_owner_id(dataset_name)
            if owner:
                load_user_id = owner

        dataset = _load_dataset(
            dataset_name,
            load_user_id,
            image_id_min=image_id_min,
            image_id_max=image_id_max,
        )
        if not dataset:
            raise ValueError(f"Dataset '{dataset_name}' not found")

        job_service.update_job(job_id, total=len(dataset["images"]))

        buf = ExportService.build_zip(
            dataset,
            prefix,
            fmt,
            split_dataset=split_dataset,
            train_split=train_split,
            val_split=val_split,
            test_split=test_split,
        )

        out_dir = os.path.join(settings.EXPORT_DIR, job_id)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, out_name)
        with open(out_path, "wb") as fh:
            fh.write(buf.getvalue())

        size = os.path.getsize(out_path)
        result_url = f"/api/export/download/{job_id}"
        job_service.mark_succeeded(
            job_id,
            processed=len(dataset["images"]),
            result_url=result_url,
            zip_size=size,
            zip_path=out_path,
        )
        _update_history(
            history_id,
            status="completed",
            filename=out_name,
            image_count=len(dataset["images"]),
            file_size=size,
        )
        logger.info(f"[export-job {job_id}] done — {size:,} bytes at {out_path}")
        return {"job_id": job_id, "result_url": result_url, "size": size}

    except Exception as exc:
        logger.exception(f"[export-job {job_id}] FAILED: {exc}")
        job_service.mark_failed(job_id, str(exc))
        _update_history(history_id, status="failed", error=str(exc)[:500])
        raise
