"""
Async upload pipeline — Celery tasks for ingesting up to 100 k images.

Architecture
------------
1. API endpoint streams uploaded bytes into ``STAGING_DIR/<job_id>/`` as
   files arrive (no buffering in memory).
2. Endpoint enqueues `process_upload_job(job_id, ...)` which runs here.
3. Worker probes every image (PIL → width/height), moves files to the
   permanent upload directory, inserts metadata into MongoDB using the
   synchronous PyMongo driver (Motor is asyncio-only and incompatible
   with Celery's prefork pool).
4. Progress is published to Redis (`job_service`) every batch so the
   frontend can show a live progress bar and auto-redirect on completion.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import uuid
import concurrent.futures
import threading
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import yaml
from bson import ObjectId
from celery.utils.log import get_task_logger
from PIL import Image as PILImage
from pymongo import MongoClient

from app.config.settings import settings
from app.services import job_service
from app.tasks.celery_app import celery_app

logger = get_task_logger(__name__)


# ---------------------------------------------------------------------------
# Sync Mongo client (one per worker process)
# ---------------------------------------------------------------------------
_mongo_client: Optional[MongoClient] = None


def _db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(settings.MONGODB_URL)
    return _mongo_client[settings.DATABASE_NAME]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _probe_image(path: str) -> Tuple[int, int]:
    """Return ``(width, height)`` for an image on disk; (0, 0) on failure."""
    try:
        with PILImage.open(path) as img:
            return img.size
    except Exception:
        return 0, 0


def _build_image_url(stored_filename: Optional[str]) -> Optional[str]:
    return f"/uploads/images/{stored_filename}" if stored_filename else None


def _process_yolo_upload(
    *,
    job_id: str,
    yaml_path: str,
    labels_path: str,
    image_records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Convert YOLO format to COCO format.
    
    Extracts categories from YAML and annotations from the ZIP file.
    Returns a COCO-formatted dict with categories and annotations.
    
    Note: Uses sequential numeric IDs for images to ensure proper mapping to database IDs.
    """
    # Parse YAML to get category names
    category_names = []
    with open(yaml_path, "r", encoding="utf-8") as f:
        yolo_data = yaml.safe_load(f)
        if yolo_data:
            names = yolo_data.get("names", [])
            if isinstance(names, dict):
                # Some versions use dict: {0: 'cat', 1: 'dog'}
                category_names = [names[k] for k in sorted(names.keys())]
            else:
                category_names = names if isinstance(names, list) else []
    
    if not category_names:
        logger.warning(f"[upload-job {job_id}] No category names found in YAML")
        return {"categories": [], "images": [], "annotations": []}
    
    # Create categories list
    categories = [
        {
            "id": idx,
            "name": name,
            "supercategory": "object",
        }
        for idx, name in enumerate(category_names)
    ]
    
    # Build image map: original filename (without extension) → (image record, coco_id)
    img_map = {}
    coco_images = []
    for coco_id, rec in enumerate(image_records, start=1):
        base = os.path.splitext(rec["original"])[0]
        img_map[base] = {
            "original": rec["original"],
            "width": rec["width"],
            "height": rec["height"],
            "coco_id": coco_id,
        }
        coco_images.append({
            "id": coco_id,
            "file_name": rec["original"],
            "width": rec["width"],
            "height": rec["height"],
        })
    
    # Extract annotations from ZIP
    annotations = []
    next_ann_id = 1
    
    try:
        with zipfile.ZipFile(labels_path, "r") as zf:
            txt_entries = [
                e for e in zf.infolist()
                if not e.is_dir() and e.filename.endswith(".txt")
                and os.path.basename(e.filename) != "classes.txt"
            ]
            
            for entry in txt_entries:
                # Match label file to image by basename
                label_basename = os.path.splitext(os.path.basename(entry.filename))[0]
                img_info = img_map.get(label_basename)
                
                if not img_info:
                    logger.debug(f"[upload-job {job_id}] No image match for label {entry.filename}")
                    continue
                
                img_w = img_info["width"]
                img_h = img_info["height"]
                img_coco_id = img_info["coco_id"]
                
                # Parse YOLO annotations
                with zf.open(entry) as f:
                    content = f.read().decode("utf-8")
                
                for line in content.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        try:
                            class_idx = int(parts[0])
                            xc = float(parts[1])
                            yc = float(parts[2])
                            nw = float(parts[3])
                            nh = float(parts[4])
                            
                            # Convert normalized YOLO coords to absolute COCO bbox [x, y, w, h]
                            w = nw * img_w
                            h = nh * img_h
                            x = (xc - nw / 2) * img_w
                            y = (yc - nh / 2) * img_h
                            
                            # Clamp to image bounds
                            x = max(0, min(x, img_w))
                            y = max(0, min(y, img_h))
                            w = max(0, min(w, img_w - x))
                            h = max(0, min(h, img_h - y))
                            
                            annotations.append({
                                "id": next_ann_id,
                                "image_id": img_coco_id,
                                "category_id": class_idx if class_idx < len(category_names) else 0,
                                "bbox": [x, y, w, h],
                                "area": w * h,
                                "iscrowd": 0,
                            })
                            next_ann_id += 1
                        except (ValueError, IndexError):
                            continue
    except Exception as exc:
        logger.error(f"[upload-job {job_id}] Failed to extract YOLO annotations: {exc}")
        raise
    
    logger.info(
        f"[upload-job {job_id}] YOLO: {len(categories)} categories, "
        f"{len(annotations)} annotations"
    )
    
    return {
        "categories": categories,
        "images": coco_images,
        "annotations": annotations,
    }


# ---------------------------------------------------------------------------
# Main task
# ---------------------------------------------------------------------------
@celery_app.task(
    name="upload.process_upload_job",
    bind=True,
    autoretry_for=(IOError, OSError),
    retry_kwargs={"max_retries": 3, "countdown": 10},
)
def process_upload_job(
    self,
    job_id: str,
    user_id: str,
    dataset_name: str,
    staging_dir: str,
    manifest_filename: str = "manifest.json",
    mode: str = "create",
    annotation_format: Optional[str] = None,
) -> Dict[str, Any]:
    """Promote a staged upload batch to a fully-ingested dataset.

    The staging directory is expected to contain:
        manifest.json          — list of ``{original, stored}`` pairs
        annotation.json        — optional COCO JSON file
        <stored_filename> ...  — every uploaded image, already renamed

    On success the dataset is queryable at /api/annotations/{dataset_name}.
    """
    logger.info(f"[upload-job {job_id}] start — dataset={dataset_name}")
    job_service.mark_running(job_id)

    try:
        manifest_path = os.path.join(staging_dir, manifest_filename)
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest: List[Dict[str, str]] = json.load(fh)

        target_dir = os.path.join(settings.UPLOAD_DIR, "images")
        os.makedirs(target_dir, exist_ok=True)

        job_service.update_job(job_id, total=len(manifest))

        # ── Phase 1: probe + move each image ─────────────────────────────
        filename_map: Dict[str, str] = {}                     # original → stored
        image_records: List[Dict[str, Any]] = []
        failed: List[str] = []

        def _process_single_image(entry: Dict[str, str]) -> Dict[str, Any]:
            original = entry["original"]
            stored = entry["stored"]
            src = os.path.join(staging_dir, stored)
            dst = os.path.join(target_dir, stored)

            if not os.path.isfile(src):
                return {"status": "failed", "original": original, "reason": "file_not_found"}

            try:
                w, h = _probe_image(src)
                shutil.move(src, dst)
                return {
                    "status": "success",
                    "original": original,
                    "stored": stored,
                    "width": w,
                    "height": h,
                    "size": os.path.getsize(dst),
                }
            except Exception as exc:
                logger.warning(f"[upload-job {job_id}] failed {original}: {exc}")
                return {"status": "failed", "original": original, "reason": str(exc)}

        max_workers = min(8, len(manifest)) if manifest else 1
        completed_since_last_update = 0
        total_manifest = len(manifest)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_single_image, entry): entry for entry in manifest}
            
            for idx, fut in enumerate(concurrent.futures.as_completed(futures), start=1):
                res = fut.result()
                if res["status"] == "success":
                    filename_map[res["original"]] = res["stored"]
                    image_records.append({
                        "stored": res["stored"],
                        "original": res["original"],
                        "width": res["width"],
                        "height": res["height"],
                        "size": res["size"],
                    })
                else:
                    failed.append(res["original"])
                
                completed_since_last_update += 1
                if completed_since_last_update >= 50 or idx == total_manifest:
                    job_service.increment_processed(job_id, completed_since_last_update, total=total_manifest)
                    completed_since_last_update = 0

        # ── Phase 2: persist dataset to Mongo ────────────────────────────
        ann_path = os.path.join(staging_dir, "annotation.json")
        coco_data: Optional[Dict[str, Any]] = None
        if os.path.isfile(ann_path):
            try:
                with open(ann_path, "r", encoding="utf-8") as fh:
                    coco_data = json.load(fh)
            except Exception as exc:
                logger.warning(f"[upload-job {job_id}] annotation.json unreadable: {exc}")

        # ── Phase 2.5: Handle YOLO format ────────────────────────────────
        if annotation_format == "yolo":
            yaml_path = os.path.join(staging_dir, "yolo_data.yaml")
            labels_path = os.path.join(staging_dir, "yolo_labels.zip")
            if os.path.isfile(yaml_path) and os.path.isfile(labels_path):
                try:
                    coco_data = _process_yolo_upload(
                        job_id=job_id,
                        yaml_path=yaml_path,
                        labels_path=labels_path,
                        image_records=image_records,
                    )
                except Exception as exc:
                    logger.warning(f"[upload-job {job_id}] YOLO processing failed: {exc}")

        _persist_dataset(
            user_id=user_id,
            dataset_name=dataset_name,
            image_records=image_records,
            filename_map=filename_map,
            coco_data=coco_data,
            mode=mode,
        )

        # ── Phase 3: cleanup staging dir ─────────────────────────────────
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception:
            pass

        job_service.mark_succeeded(
            job_id,
            processed=len(image_records),
            total=len(manifest),
            failed=len(failed),
            dataset_name=dataset_name,
        )
        logger.info(
            f"[upload-job {job_id}] done — saved={len(image_records)} "
            f"failed={len(failed)}"
        )
        return {
            "job_id": job_id,
            "dataset_name": dataset_name,
            "saved": len(image_records),
            "failed": failed,
        }

    except Exception as exc:
        logger.exception(f"[upload-job {job_id}] FAILED: {exc}")
        job_service.mark_failed(job_id, str(exc))
        raise


# ---------------------------------------------------------------------------
# Dataset persistence (sync PyMongo)
# ---------------------------------------------------------------------------
def _persist_dataset(
    *,
    user_id: str,
    dataset_name: str,
    image_records: List[Dict[str, Any]],
    filename_map: Dict[str, str],
    coco_data: Optional[Dict[str, Any]],
    mode: str = "create",
) -> None:
    """Persist images, categories, and annotations using PyMongo (sync).

    mode='create' (default): retire any prior copy and create a fresh dataset.
    mode='append': add the new images (and any extra COCO annotations) to the
        existing active dataset with this name and user_id. If no existing
        dataset is found, falls back to 'create'.
    """
    db = _db()
    meta_col = db["dataset_metadata"]
    img_col = db["images"]
    cat_col = db["categories"]
    ann_col = db["annotations"]

    existing = meta_col.find_one(
        {"dataset_name": dataset_name, "user_id": user_id, "is_active": True}
    )

    now = datetime.utcnow()

    append_mode = (mode == "append") and existing is not None

    if append_mode:
        # Reuse existing dataset_id; continue id sequences from current max.
        dataset_id = existing["_id"]
        max_img = img_col.find(
            {"dataset_id": dataset_id}, {"image_id": 1}
        ).sort("image_id", -1).limit(1)
        max_img_doc = next(iter(max_img), None)
        start_image_id = (max_img_doc["image_id"] + 1) if max_img_doc else 1

        max_ann = ann_col.find(
            {"dataset_id": dataset_id}, {"annotation_id": 1}
        ).sort("annotation_id", -1).limit(1)
        max_ann_doc = next(iter(max_ann), None)
        ann_id_offset = (max_ann_doc["annotation_id"]) if max_ann_doc else 0

        existing_cat_ids = {
            c["category_id"]
            for c in cat_col.find(
                {"dataset_id": dataset_id}, {"category_id": 1}
            )
        }
    else:
        # Soft-delete any prior copy with the same name for this user.
        if existing:
            meta_col.update_one(
                {"_id": existing["_id"]}, {"$set": {"is_active": False}}
            )
        dataset_id = str(ObjectId())
        start_image_id = 1
        ann_id_offset = 0
        existing_cat_ids = set()

    categories = (coco_data or {}).get("categories", []) or []
    images = (coco_data or {}).get("images", []) or []
    annotations = (coco_data or {}).get("annotations", []) or []

    # ── Build the master image list ──────────────────────────────────────
    # Start from the COCO JSON images; fall back to images-only uploads.
    coco_by_filename = {img["file_name"]: img for img in images if "file_name" in img}

    image_docs: List[Dict[str, Any]] = []
    next_image_id = start_image_id
    coco_id_remap: Dict[int, int] = {}  # original COCO id → assigned id

    for rec in image_records:
        coco_img = coco_by_filename.get(rec["original"])
        if coco_img and not append_mode:
            assigned_id = coco_img.get("id", next_image_id)
            coco_id_remap[coco_img["id"]] = assigned_id
            width = coco_img.get("width") or rec["width"]
            height = coco_img.get("height") or rec["height"]
            extra = coco_img.get("extra") or {}
            date_captured = coco_img.get("date_captured", "")
        elif coco_img and append_mode:
            # In append mode, always assign a fresh id to avoid collisions.
            assigned_id = next_image_id
            coco_id_remap[coco_img["id"]] = assigned_id
            width = coco_img.get("width") or rec["width"]
            height = coco_img.get("height") or rec["height"]
            extra = coco_img.get("extra") or {}
            date_captured = coco_img.get("date_captured", "")
        else:
            assigned_id = next_image_id
            width = rec["width"]
            height = rec["height"]
            extra = {}
            date_captured = ""

        image_docs.append({
            "user_id": user_id,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "image_id": assigned_id,
            "file_name": rec["original"],
            "stored_filename": rec["stored"],
           "file_path": os.path.join(settings.UPLOAD_DIR, "images", rec["stored"]),
            "image_url": _build_image_url(rec["stored"]),
            "width": width,
            "height": height,
            "date_captured": date_captured,
            "extra": extra,
            "uploaded_at": now,
        })
        next_image_id = max(next_image_id, assigned_id) + 1

    # Bulk insert images.
    if image_docs:
        img_col.insert_many(image_docs)

    # ── Categories ───────────────────────────────────────────────────────
    if categories:
        new_cats = [c for c in categories if c["id"] not in existing_cat_ids]
        if new_cats:
            cat_col.insert_many([
                {
                    "user_id": user_id,
                    "dataset_id": dataset_id,
                    "dataset_name": dataset_name,
                    "category_id": c["id"],
                    "name": c["name"],
                    "supercategory": c.get("supercategory", ""),
                    "created_at": now,
                }
                for c in new_cats
            ])

    # ── Annotations ──────────────────────────────────────────────────────
    if annotations:
        ann_docs = []
        for a in annotations:
            image_id = coco_id_remap.get(a["image_id"], a["image_id"])
            assigned_ann_id = (a["id"] + ann_id_offset) if append_mode else a["id"]
            ann_docs.append({
                "user_id": user_id,
                "dataset_id": dataset_id,
                "dataset_name": dataset_name,
                "annotation_id": assigned_ann_id,
                "image_id": image_id,
                "category_id": a["category_id"],
                "bbox": a["bbox"],
                "area": a.get("area") or (a["bbox"][2] * a["bbox"][3] if len(a.get("bbox", [])) == 4 else 0),
                "segmentation": a.get("segmentation", []),
                "iscrowd": a.get("iscrowd", 0),
                "uploaded_at": now,
                "updated_at": now,
            })
        if ann_docs:
            ann_col.insert_many(ann_docs)

    # ── Metadata ─────────────────────────────────────────────────────────
    if append_mode:
        new_cats_count = len([c for c in categories if c["id"] not in existing_cat_ids]) if categories else 0
        meta_col.update_one(
            {"_id": dataset_id},
            {
                "$inc": {
                    "total_images": len(image_docs),
                    "total_annotations": len(annotations),
                    "total_categories": new_cats_count,
                },
                "$set": {"updated_at": now},
            },
        )
    else:
        meta_col.insert_one({
            "_id": dataset_id,
            "user_id": user_id,
            "dataset_name": dataset_name,
            "total_categories": len(categories),
            "total_images": len(image_docs),
            "total_annotations": len(annotations),
            "uploaded_at": now,
            "updated_at": now,
            "is_active": True,
        })


# ---------------------------------------------------------------------------
# ZIP extraction helpers
# ---------------------------------------------------------------------------

def _insert_coco_annotations_zip(
    *,
    db,
    dataset_id: str,
    user_id: str,
    dataset_name: str,
    coco_data: Dict[str, Any],
    filename_to_image_id: Dict[str, int],
) -> int:
    """Insert COCO categories and annotations for a ZIP-extracted dataset.

    ``filename_to_image_id`` maps each image's original basename to the
    assigned ``image_id`` stored in MongoDB — built during the extraction loop.

    Returns the number of annotation documents inserted.
    """
    now = datetime.utcnow()
    cat_col = db["categories"]
    ann_col = db["annotations"]
    meta_col = db["dataset_metadata"]

    categories = coco_data.get("categories") or []
    coco_images = coco_data.get("images") or []
    annotations = coco_data.get("annotations") or []

    # ── Categories ────────────────────────────────────────────────────────
    if categories:
        existing_cat_ids = {
            c["category_id"]
            for c in cat_col.find({"dataset_id": dataset_id}, {"category_id": 1})
        }
        new_cats = [c for c in categories if c.get("id") not in existing_cat_ids]
        if new_cats:
            cat_col.insert_many([
                {
                    "user_id": user_id,
                    "dataset_id": dataset_id,
                    "dataset_name": dataset_name,
                    "category_id": c["id"],
                    "name": c["name"],
                    "supercategory": c.get("supercategory", ""),
                    "created_at": now,
                }
                for c in new_cats
            ])

    # ── Map COCO image_id → assigned MongoDB image_id ────────────────────
    coco_id_to_assigned: Dict[int, int] = {}
    for ci in coco_images:
        fname = os.path.basename(ci.get("file_name", ""))
        if fname in filename_to_image_id:
            coco_id_to_assigned[ci["id"]] = filename_to_image_id[fname]

    # ── Annotations ───────────────────────────────────────────────────────
    inserted = 0
    if annotations:
        ann_docs = []
        for a in annotations:
            assigned_image_id = coco_id_to_assigned.get(a.get("image_id"))
            if assigned_image_id is None:
                continue
            bbox = a.get("bbox") or []
            ann_docs.append({
                "user_id": user_id,
                "dataset_id": dataset_id,
                "dataset_name": dataset_name,
                "annotation_id": a["id"],
                "image_id": assigned_image_id,
                "category_id": a.get("category_id"),
                "bbox": bbox,
                "area": a.get("area") or (bbox[2] * bbox[3] if len(bbox) == 4 else 0),
                "segmentation": a.get("segmentation") or [],
                "iscrowd": a.get("iscrowd", 0),
                "uploaded_at": now,
                "updated_at": now,
            })
        if ann_docs:
            ann_col.insert_many(ann_docs, ordered=False)
            inserted = len(ann_docs)
            # Mark annotated images
            annotated_img_ids = list({d["image_id"] for d in ann_docs})
            db["images"].update_many(
                {"dataset_id": dataset_id, "image_id": {"$in": annotated_img_ids}},
                {"$set": {"is_annotated": True}},
            )

    meta_col.update_one(
        {"_id": dataset_id},
        {"$set": {"total_categories": len(categories), "updated_at": datetime.utcnow()}},
    )
    return inserted


# ---------------------------------------------------------------------------
# ZIP extraction Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="upload.process_zip_upload_job",
    bind=True,
    autoretry_for=(IOError, OSError),
    retry_kwargs={"max_retries": 3, "countdown": 10},
)
def process_zip_upload_job(
    self,
    job_id: str,
    user_id: str,
    dataset_name: str,
    staging_dir: str,
    zip_filename: str = "upload.zip",
    annotation_filename: Optional[str] = None,
    mode: str = "create",
    annotation_format: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract a ZIP archive and progressively ingest images into MongoDB.

    The dataset metadata record is created *before* any images are extracted
    so the annotation UI can open immediately while extraction is still in
    progress.  Images appear in the UI in batches of 500.

    Annotation sources (checked in priority order):
        1. ``annotation_filename`` — external COCO JSON uploaded alongside ZIP.
        2. ``_annotations.json`` / ``annotations.json`` embedded at the ZIP root.
    """
    import zipfile as _zipfile

    _VALID_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    BATCH_SIZE = 500

    logger.info(f"[zip-job {job_id}] start — dataset={dataset_name} mode={mode}")
    job_service.mark_running(job_id)

    zip_path = os.path.join(staging_dir, zip_filename)
    target_dir = os.path.join(settings.UPLOAD_DIR, "images")
    os.makedirs(target_dir, exist_ok=True)

    try:
        # ── Phase 1: scan ZIP — count images & find embedded annotations ──
        with _zipfile.ZipFile(zip_path, "r") as zf:
            all_entries = zf.infolist()

        image_entries = [
            e for e in all_entries
            if not e.is_dir()
            and os.path.splitext(os.path.basename(e.filename))[1].lower() in _VALID_EXTS
            and os.path.basename(e.filename)  # skip entries with an empty basename
        ]

        # Look for an embedded annotation file only when none was supplied externally
        embedded_ann_entry: Optional[str] = None
        if annotation_filename is None:
            for e in all_entries:
                bname = os.path.basename(e.filename).lower()
                if bname in ("_annotations.json", "annotations.json") and not e.is_dir():
                    embedded_ann_entry = e.filename
                    break

        total_images = len(image_entries)
        logger.info(f"[zip-job {job_id}] {total_images} images found in ZIP")
        job_service.update_job(job_id, total=total_images)

        if total_images == 0:
            job_service.mark_failed(job_id, "No valid images found in ZIP archive")
            shutil.rmtree(staging_dir, ignore_errors=True)
            return {"job_id": job_id, "error": "No valid images in ZIP"}

        # ── Phase 2: create / look up dataset record upfront ──────────────
        db = _db()
        meta_col = db["dataset_metadata"]
        img_col = db["images"]

        existing = meta_col.find_one(
            {"dataset_name": dataset_name, "user_id": user_id, "is_active": True}
        )
        now = datetime.utcnow()
        append_mode = (mode == "append") and (existing is not None)

        if append_mode:
            dataset_id = existing["_id"]
            max_doc = next(
                iter(
                    img_col.find(
                        {"dataset_id": dataset_id}, {"image_id": 1}
                    ).sort("image_id", -1).limit(1)
                ),
                None,
            )
            start_image_id = (max_doc["image_id"] + 1) if max_doc else 1
        else:
            if existing:
                meta_col.update_one(
                    {"_id": existing["_id"]}, {"$set": {"is_active": False}}
                )
            dataset_id = str(ObjectId())
            start_image_id = 1
            # Insert the metadata record NOW so the UI can open the dataset
            # while images are still being extracted.
            meta_col.insert_one({
                "_id": dataset_id,
                "user_id": user_id,
                "dataset_name": dataset_name,
                "total_categories": 0,
                "total_images": 0,
                "total_annotations": 0,
                "is_active": True,
                "status": "processing",
                "uploaded_at": now,
                "updated_at": now,
            })

        # ── Phase 3: extract + ingest images in streaming batches ─────────
        failed: List[str] = []
        saved_count = 0
        next_image_id = start_image_id
        # Maps original filename → assigned image_id (used for annotation linking)
        filename_to_image_id: Dict[str, int] = {}

        thread_local = threading.local()

        def _get_thread_zip():
            if not hasattr(thread_local, "zf"):
                thread_local.zf = _zipfile.ZipFile(zip_path, "r")
            return thread_local.zf

        def _cleanup_thread_zip():
            if hasattr(thread_local, "zf"):
                try:
                    thread_local.zf.close()
                except Exception:
                    pass

        def _process_single_zip_entry(entry, assigned_image_id):
            original_name = os.path.basename(entry.filename)
            ext = os.path.splitext(original_name)[1].lower()
            stored = f"{uuid.uuid4().hex}{ext}"
            dst = os.path.join(target_dir, stored)
            try:
                zf = _get_thread_zip()
                with zf.open(entry) as src_f:
                    with open(dst, "wb") as dst_f:
                        shutil.copyfileobj(src_f, dst_f)
                
                w, h = _probe_image(dst)
                doc = {
                    "user_id": user_id,
                    "dataset_id": dataset_id,
                    "dataset_name": dataset_name,
                    "image_id": assigned_image_id,
                    "file_name": original_name,
                    "stored_filename": stored,
                    "file_path": dst,
                    "image_url": _build_image_url(stored),
                    "width": w,
                    "height": h,
                    "date_captured": "",
                    "extra": {},
                    "is_annotated": False,
                    "uploaded_at": now,
                }
                return {"status": "success", "doc": doc, "original_name": original_name, "image_id": assigned_image_id}
            except Exception as exc:
                logger.warning(f"[zip-job {job_id}] failed {original_name}: {exc}")
                return {"status": "failed", "original_name": original_name, "error": str(exc)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            for batch_start in range(0, total_images, BATCH_SIZE):
                batch = image_entries[batch_start: batch_start + BATCH_SIZE]
                batch_docs: List[Dict[str, Any]] = []

                futures = []
                for offset, entry in enumerate(batch):
                    assigned_id = next_image_id + offset
                    fut = executor.submit(_process_single_zip_entry, entry, assigned_id)
                    futures.append(fut)

                concurrent.futures.wait(futures)

                for fut in futures:
                    res = fut.result()
                    if res["status"] == "success":
                        doc = res["doc"]
                        batch_docs.append(doc)
                        filename_to_image_id[res["original_name"]] = res["image_id"]
                    else:
                        failed.append(res["original_name"])

                next_image_id += len(batch)

                if batch_docs:
                    img_col.insert_many(batch_docs, ordered=False)
                    saved_count += len(batch_docs)
                    meta_col.update_one(
                        {"_id": dataset_id},
                        {"$set": {
                            "total_images": saved_count,
                            "updated_at": datetime.utcnow(),
                        }},
                    )

                job_service.increment_processed(job_id, len(batch), total=total_images)
                logger.debug(
                    f"[zip-job {job_id}] batch {batch_start // BATCH_SIZE + 1}: "
                    f"saved {len(batch_docs)}, cumulative={saved_count}"
                )

            # Clean up all thread-local ZipFile readers
            cleanup_futures = [executor.submit(_cleanup_thread_zip) for _ in range(8)]
            concurrent.futures.wait(cleanup_futures)

        # ── Phase 4: process COCO annotations ─────────────────────────────
        coco_data: Optional[Dict[str, Any]] = None
        if annotation_filename and annotation_filename != "yolo":
            ann_path = os.path.join(staging_dir, annotation_filename)
            if os.path.isfile(ann_path):
                try:
                    with open(ann_path, "r", encoding="utf-8") as fh:
                        coco_data = json.load(fh)
                except Exception as exc:
                    logger.warning(f"[zip-job {job_id}] annotation read failed: {exc}")
        elif embedded_ann_entry:
            with _zipfile.ZipFile(zip_path, "r") as zf:
                try:
                    with zf.open(embedded_ann_entry) as f:
                        coco_data = json.load(f)
                except Exception as exc:
                    logger.warning(f"[zip-job {job_id}] embedded annotation failed: {exc}")

        # ── Phase 4.5: Handle YOLO format ───────────────────────────────
        if annotation_format == "yolo" and annotation_filename == "yolo":
            yaml_path = os.path.join(staging_dir, "yolo_data.yaml")
            labels_path = os.path.join(staging_dir, "yolo_labels.zip")
            if os.path.isfile(yaml_path) and os.path.isfile(labels_path):
                try:
                    # Create image records for YOLO processing
                    image_records = [
                        {
                            "original": img_col.find_one(
                                {"dataset_id": dataset_id, "image_id": filename_to_image_id.get(orig)},
                                {"file_name": 1}
                            )["file_name"] if orig in filename_to_image_id else orig,
                            "width": img_col.find_one(
                                {"dataset_id": dataset_id, "image_id": filename_to_image_id.get(orig)},
                                {"width": 1}
                            )["width"] if orig in filename_to_image_id else 0,
                            "height": img_col.find_one(
                                {"dataset_id": dataset_id, "image_id": filename_to_image_id.get(orig)},
                                {"height": 1}
                            )["height"] if orig in filename_to_image_id else 0,
                        }
                        for orig in filename_to_image_id.keys()
                    ]
                    coco_data = _process_yolo_upload(
                        job_id=job_id,
                        yaml_path=yaml_path,
                        labels_path=labels_path,
                        image_records=image_records,
                    )
                except Exception as exc:
                    logger.warning(f"[zip-job {job_id}] YOLO processing failed: {exc}")

        ann_count = 0
        if coco_data:
            ann_count = _insert_coco_annotations_zip(
                db=db,
                dataset_id=dataset_id,
                user_id=user_id,
                dataset_name=dataset_name,
                coco_data=coco_data,
                filename_to_image_id=filename_to_image_id,
            )

        # ── Phase 5: finalise dataset metadata ────────────────────────────
        meta_col.update_one(
            {"_id": dataset_id},
            {"$set": {
                "status": "ready",
                "total_images": saved_count,
                "total_annotations": ann_count,
                "updated_at": datetime.utcnow(),
            }},
        )

        # ── Cleanup staging dir ────────────────────────────────────────────
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception:
            pass

        job_service.mark_succeeded(
            job_id,
            processed=saved_count,
            total=total_images,
            failed=len(failed),
            dataset_name=dataset_name,
        )
        logger.info(
            f"[zip-job {job_id}] done — saved={saved_count} "
            f"annotations={ann_count} failed={len(failed)}"
        )
        return {
            "job_id": job_id,
            "dataset_name": dataset_name,
            "saved": saved_count,
            "annotations": ann_count,
            "failed": failed[:50],
        }

    except Exception as exc:
        logger.exception(f"[zip-job {job_id}] FAILED: {exc}")
        job_service.mark_failed(job_id, str(exc))
        raise

# ---------------------------------------------------------------------------
# YOLO ZIP extraction Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    name="upload.process_yolo_upload_job",
    bind=True,
    autoretry_for=(IOError, OSError),
    retry_kwargs={"max_retries": 3, "countdown": 10},
)
def process_yolo_upload_job(
    self,
    job_id: str,
    user_id: str,
    dataset_name: str,
    staging_dir: str,
    zip_filename: str = "yolo_upload.zip",
) -> Dict[str, Any]:
    """Extract a YOLO ZIP archive and ingest annotations into MongoDB."""
    import zipfile as _zipfile
    import yaml

    logger.info(f"[yolo-job {job_id}] start — dataset={dataset_name}")
    job_service.mark_running(job_id)

    zip_path = os.path.join(staging_dir, zip_filename)
    
    db = _db()
    cat_col = db["categories"]
    img_col = db["images"]
    ann_col = db["annotations"]
    meta_col = db["dataset_metadata"]

    existing = meta_col.find_one({"dataset_name": dataset_name, "user_id": user_id, "is_active": True})
    if not existing:
        error_msg = f"Dataset {dataset_name} not found or inactive."
        job_service.mark_failed(job_id, error_msg)
        raise ValueError(error_msg)
        
    dataset_id = existing["_id"]

    try:
        with _zipfile.ZipFile(zip_path, "r") as zf:
            all_entries = zf.infolist()
            
            # Find data.yaml
            yaml_entry = None
            for e in all_entries:
                if os.path.basename(e.filename) in ("data.yaml", "dataset.yaml", "data.yml"):
                    yaml_entry = e.filename
                    break
                    
            category_names = []
            if yaml_entry:
                with zf.open(yaml_entry) as f:
                    yolo_data = yaml.safe_load(f)
                category_names = yolo_data.get("names", [])
                if isinstance(category_names, dict):
                    # Some versions use dict: {0: 'cat', 1: 'dog'}
                    category_names = [category_names[k] for k in sorted(category_names.keys())]
            
            # Create categories if needed
            yolo_to_mongo_cat_id = {}
            if category_names:
                existing_cats = list(cat_col.find({"dataset_id": dataset_id}))
                cat_name_to_id = {c["name"]: c["category_id"] for c in existing_cats}
                
                max_cat_id = max([c["category_id"] for c in existing_cats]) if existing_cats else 0
                now = datetime.utcnow()
                
                new_cat_docs = []
                for idx, name in enumerate(category_names):
                    if name in cat_name_to_id:
                        yolo_to_mongo_cat_id[idx] = cat_name_to_id[name]
                    else:
                        max_cat_id += 1
                        yolo_to_mongo_cat_id[idx] = max_cat_id
                        new_cat_docs.append({
                            "user_id": user_id,
                            "dataset_id": dataset_id,
                            "dataset_name": dataset_name,
                            "category_id": max_cat_id,
                            "name": name,
                            "supercategory": "object",
                            "created_at": now,
                        })
                
                if new_cat_docs:
                    cat_col.insert_many(new_cat_docs)
            
            # Get existing images mapped by basename (no extension)
            images = list(img_col.find({"dataset_id": dataset_id}))
            img_map = {}
            for img in images:
                base = os.path.splitext(img["file_name"])[0]
                img_map[base] = img
            
            # Find label txt files
            txt_entries = [
                e for e in all_entries
                if not e.is_dir() and e.filename.endswith(".txt") and os.path.basename(e.filename) != "classes.txt"
            ]
            
            job_service.update_job(job_id, total=len(txt_entries))
            
            max_ann = ann_col.find({"dataset_id": dataset_id}, {"annotation_id": 1}).sort("annotation_id", -1).limit(1)
            max_ann_doc = next(iter(max_ann), None)
            next_ann_id = (max_ann_doc["annotation_id"] + 1) if max_ann_doc else 1
            
            annotations_to_insert = []
            failed_files = []
            processed_count = 0
            
            now = datetime.utcnow()
            for e in txt_entries:
                basename = os.path.splitext(os.path.basename(e.filename))[0]
                matched_img = img_map.get(basename)
                if not matched_img:
                    failed_files.append(e.filename)
                    processed_count += 1
                    continue
                
                with zf.open(e) as f:
                    content = f.read().decode("utf-8")
                
                img_w = matched_img["width"]
                img_h = matched_img["height"]
                img_id = matched_img["image_id"]
                
                for line in content.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        try:
                            class_idx = int(parts[0])
                            xc = float(parts[1])
                            yc = float(parts[2])
                            nw = float(parts[3])
                            nh = float(parts[4])
                            
                            # Convert to absolute coords
                            w = nw * img_w
                            h = nh * img_h
                            x = (xc - nw / 2) * img_w
                            y = (yc - nh / 2) * img_h
                            
                            cat_id = yolo_to_mongo_cat_id.get(class_idx, class_idx)
                            
                            annotations_to_insert.append({
                                "user_id": user_id,
                                "dataset_id": dataset_id,
                                "dataset_name": dataset_name,
                                "annotation_id": next_ann_id,
                                "image_id": img_id,
                                "category_id": cat_id,
                                "bbox": [x, y, w, h],
                                "area": w * h,
                                "segmentation": [],
                                "iscrowd": 0,
                                "uploaded_at": now,
                                "updated_at": now,
                            })
                            next_ann_id += 1
                        except ValueError:
                            pass
                
                processed_count += 1
                if processed_count % 100 == 0:
                    job_service.increment_processed(job_id, 100, total=len(txt_entries))
                    
            if annotations_to_insert:
                # Insert in batches
                batch_size = 5000
                for i in range(0, len(annotations_to_insert), batch_size):
                    ann_col.insert_many(annotations_to_insert[i:i+batch_size])
                
                # Mark images as annotated
                annotated_img_ids = list({a["image_id"] for a in annotations_to_insert})
                img_col.update_many(
                    {"dataset_id": dataset_id, "image_id": {"$in": annotated_img_ids}},
                    {"$set": {"is_annotated": True}},
                )
                
            # Update dataset metadata
            meta_col.update_one(
                {"_id": dataset_id},
                {"$inc": {"total_annotations": len(annotations_to_insert)}, "$set": {"updated_at": datetime.utcnow()}}
            )
            # Re-count categories if we added new ones
            actual_cats = cat_col.count_documents({"dataset_id": dataset_id})
            meta_col.update_one({"_id": dataset_id}, {"$set": {"total_categories": actual_cats}})
            
    except Exception as exc:
        logger.exception(f"[yolo-job {job_id}] FAILED: {exc}")
        shutil.rmtree(staging_dir, ignore_errors=True)
        job_service.mark_failed(job_id, str(exc))
        raise

    try:
        shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception:
        pass

    job_service.mark_succeeded(
        job_id,
        processed=processed_count,
        total=len(txt_entries),
        failed=len(failed_files),
        dataset_name=dataset_name,
    )
    logger.info(
        f"[yolo-job {job_id}] done — annotations={len(annotations_to_insert)} failed_files={len(failed_files)}"
    )
    return {
        "job_id": job_id,
        "dataset_name": dataset_name,
        "annotations": len(annotations_to_insert),
        "failed_files": failed_files[:50],
    }
