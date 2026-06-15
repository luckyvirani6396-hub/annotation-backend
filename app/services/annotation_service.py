"""
Annotation Service - COCO dataset CRUD with Roboflow-style visualization support.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
import re
from loguru import logger
from bson import ObjectId

from app.config.database import get_collection
from app.schemas.annotation import Annotation, COCOJSON


# ==================================================================
# Helpers
# ==================================================================

def _build_image_url(stored_filename: Optional[str]) -> Optional[str]:
    """Construct relative URL for a stored image (frontend prepends API base)."""
    if not stored_filename:
        return None
    return f"/uploads/images/{stored_filename}"

def _build_thumbnail_url(stored_filename: Optional[str]) -> Optional[str]:
        """Construct URL for thumbnail version of image."""
        if not stored_filename:
            return None
        # If you have thumbnail generation, use it; otherwise use original
        return f"/uploads/thumbnails/{stored_filename}"  # Or just return original


async def _get_metadata(dataset_name: str, user_id: str) -> Optional[Dict[str, Any]]:
    return await get_collection("dataset_metadata").find_one(
        {"dataset_name": dataset_name, "user_id": user_id, "is_active": True}
    )


async def _get_metadata_any_user(dataset_name: str) -> Optional[Dict[str, Any]]:
    """Find dataset metadata by name only — for cross-user batch-mode access."""
    return await get_collection("dataset_metadata").find_one(
        {"dataset_name": dataset_name, "is_active": True}
    )


# ==================================================================
# Service
# ==================================================================

class AnnotationService:

    @staticmethod
    async def get_dataset_owner_id(dataset_name: str) -> Optional[str]:
        """Return the user_id of whoever owns this dataset (for cross-user batch access)."""
        meta = await _get_metadata_any_user(dataset_name)
        return str(meta["user_id"]) if meta else None

    # ---------------- Dataset save ----------------
    @staticmethod
    async def save_annotation_dataset(
        data: COCOJSON,
        dataset_name: str,
        user_id: str,
        filename_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Save complete COCO dataset. filename_map maps original->stored UUID name.
        
        Args:
            data: COCOJSON object with categories, images, annotations
            dataset_name: Name of the dataset
            user_id: ID of the user uploading the dataset
            filename_map: Maps original filename to stored UUID filename
        """
        try:
            filename_map = filename_map or {}

            # If the dataset name already exists for this user, soft-delete the previous one
            existing = await _get_metadata(dataset_name, user_id)
            if existing:
                await get_collection("dataset_metadata").update_one(
                    {"_id": existing["_id"]}, {"$set": {"is_active": False}}
                )

            dataset_id = str(ObjectId())
            now = datetime.utcnow()

            await get_collection("dataset_metadata").insert_one({
                "_id": dataset_id,
                "user_id": user_id,
                "dataset_name": dataset_name,
                "total_categories": len(data.categories),
                "total_images": len(data.images),
                "total_annotations": len(data.annotations),
                "uploaded_at": now,
                "updated_at": now,
                "is_active": True,
            })

            if data.categories:
                await get_collection("categories").insert_many([
                    {
                        "user_id": user_id,
                        "dataset_id": dataset_id,
                        "dataset_name": dataset_name,
                        "category_id": c.id,
                        "name": c.name,
                        "supercategory": c.supercategory,
                        "created_at": now,
                    } for c in data.categories
                ])

            if data.images:
                image_docs = []
                for img in data.images:
                    stored = filename_map.get(img.file_name)
                    image_docs.append({
                        "user_id": user_id,
                        "dataset_id": dataset_id,
                        "dataset_name": dataset_name,
                        "image_id": img.id,
                        "file_name": img.file_name,            # original name from JSON
                        "stored_filename": stored,             # actual file on disk
                        "image_url": _build_image_url(stored), # relative URL
                        "height": img.height,
                        "width": img.width,
                        "date_captured": img.date_captured,
                        "extra": img.extra or {},
                        "uploaded_at": now,
                    })
                await get_collection("images").insert_many(image_docs)

            if data.annotations:
                await get_collection("annotations").insert_many([
                    {
                        "user_id": user_id,
                        "dataset_id": dataset_id,
                        "dataset_name": dataset_name,
                        "annotation_id": a.id,
                        "image_id": a.image_id,
                        "category_id": a.category_id,
                        "bbox": a.bbox,
                        "area": a.area,
                        "segmentation": a.segmentation,
                        "iscrowd": a.iscrowd,
                        "uploaded_at": now,
                        "updated_at": now,
                    } for a in data.annotations
                ])

            logger.success(f"Dataset '{dataset_name}' saved for user {user_id}")
            return {
                "dataset_id": dataset_id,
                "dataset_name": dataset_name,
                "total_annotations": len(data.annotations),
                "total_images": len(data.images),
                "total_categories": len(data.categories),
            }
        except Exception as e:
            logger.error(f"save_annotation_dataset failed: {e}")
            raise

    # ------------------------------------------------------------------
    # Image-only upsert (Roboflow-style manual workflow)
    # ------------------------------------------------------------------
    @staticmethod
    async def add_image_to_dataset(
        dataset_name: str,
        user_id: str,
        original_filename: str,
        stored_filename: str,
        width: int,
        height: int,
    ) -> Dict[str, Any]:
        """Attach a freshly-uploaded image to a dataset, creating the dataset
        record on demand.

        Used by ``POST /api/upload/single`` (and ``/images-only``) when the
        user uploads images without any COCO JSON — the dataset will start
        with zero categories/annotations and the user will create them
        interactively in the annotation workspace.

        Returns
        -------
        ``{"dataset_id", "image_id"}``
        """
        now = datetime.utcnow()

        # Upsert the dataset metadata.
        existing = await _get_metadata(dataset_name, user_id)
        if existing:
            dataset_id = existing["_id"]
            await get_collection("dataset_metadata").update_one(
                {"_id": dataset_id},
                {"$set": {"updated_at": now}, "$inc": {"total_images": 1}},
            )
        else:
            dataset_id = str(ObjectId())
            await get_collection("dataset_metadata").insert_one({
                "_id": dataset_id,
                "user_id": user_id,
                "dataset_name": dataset_name,
                "total_categories": 0,
                "total_images": 1,
                "total_annotations": 0,
                "uploaded_at": now,
                "updated_at": now,
                "is_active": True,
            })

        # Pick the next image_id (monotonic per dataset).
        last = await (
            get_collection("images")
            .find({"dataset_name": dataset_name, "user_id": user_id})
            .sort("image_id", -1)
            .limit(1)
            .to_list(length=1)
        )
        next_image_id = (last[0]["image_id"] + 1) if last else 1

        await get_collection("images").insert_one({
            "user_id": user_id,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "image_id": next_image_id,
            "file_name": original_filename,
            "stored_filename": stored_filename,
            "image_url": _build_image_url(stored_filename),
            "height": height,
            "width": width,
            "date_captured": now.isoformat(),
            "extra": {},
            "uploaded_at": now,
        })

        logger.success(
            f"[annotation_service] image '{original_filename}' attached to "
            f"dataset '{dataset_name}' (image_id={next_image_id})"
        )
        return {"dataset_id": dataset_id, "image_id": next_image_id}

    # ---------------- Datasets list / counts / info ----------------
    @staticmethod
    async def get_all_datasets(
        user_id: str,
        skip: int = 0,
        limit: int = 100,
        search: Optional[str] = None,
        sort_by: str = "uploaded_at",
        sort_desc: bool = True,
    ) -> List[Dict[str, Any]]:
        try:
            query: Dict[str, Any] = {"user_id": user_id, "is_active": True}
            if search:
                query["dataset_name"] = {"$regex": search, "$options": "i"}

            direction = -1 if sort_desc else 1
            cursor = (
                get_collection("dataset_metadata")
                .find(query)
                .sort(sort_by, direction)
                .skip(skip)
                .limit(limit)
            )
            datasets = await cursor.to_list(length=None)
            return [
                {
                    "dataset_name": d["dataset_name"],
                    "dataset_id": str(d["_id"]),
                    "total_images": d.get("total_images", 0),
                    "total_annotations": d.get("total_annotations", 0),
                    "total_categories": d.get("total_categories", 0),
                    "uploaded_at": d.get("uploaded_at"),
                    "updated_at": d.get("updated_at"),
                }
                for d in datasets
            ]
        except Exception as e:
            logger.error(f"get_all_datasets failed: {e}")
            return []

    @staticmethod
    async def get_datasets_count(user_id: str, search: Optional[str] = None) -> int:
        query: Dict[str, Any] = {"user_id": user_id, "is_active": True}
        if search:
            query["dataset_name"] = {"$regex": search, "$options": "i"}
        return await get_collection("dataset_metadata").count_documents(query)

    @staticmethod
    async def get_dataset_info(dataset_name: str, user_id: str) -> Optional[Dict[str, Any]]:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return None
        return {
            "dataset_name": meta["dataset_name"],
            "dataset_id": str(meta["_id"]),
            "total_images": meta.get("total_images", 0),
            "total_annotations": meta.get("total_annotations", 0),
            "total_categories": meta.get("total_categories", 0),
            "uploaded_at": meta.get("uploaded_at"),
            "updated_at": meta.get("updated_at"),
        }

    @staticmethod
    async def get_annotation_dataset(
        dataset_name: str,
        user_id: str,
        image_id_min: Optional[int] = None,
        image_id_max: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return whole dataset (for export / existence check).

        Optional ``image_id_min`` / ``image_id_max`` restrict images (and their
        annotations) to a batch slice.
        """
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return None
        dataset_id = meta["_id"]

        img_range: Optional[Dict[str, int]] = None
        if image_id_min is not None or image_id_max is not None:
            img_range = {}
            if image_id_min is not None:
                img_range["$gte"] = image_id_min
            if image_id_max is not None:
                img_range["$lte"] = image_id_max

        categories = await get_collection("categories").find(
            {"user_id": user_id, "dataset_id": dataset_id}
        ).to_list(length=None)

        img_query: Dict[str, Any] = {"user_id": user_id, "dataset_id": dataset_id}
        if img_range:
            img_query["image_id"] = img_range
        images = await get_collection("images").find(img_query).to_list(length=None)

        ann_query: Dict[str, Any] = {"user_id": user_id, "dataset_id": dataset_id}
        if img_range:
            ann_query["image_id"] = img_range
        annotations = await get_collection("annotations").find(ann_query).to_list(length=None)

        return {
            "metadata": {**meta, "_id": str(meta["_id"])},
            "categories": [
                {
                    "id": c["category_id"],
                    "name": c["name"],
                    "supercategory": c.get("supercategory", ""),
                } for c in categories
            ],
            "images": [
                {
                    "id": i["image_id"],
                    "file_name": i["file_name"],
                    "stored_filename": i.get("stored_filename"),
                    "file_path": i.get("file_path"),
                    "image_url": i.get("image_url") or _build_image_url(i.get("stored_filename")),
                    "width": i["width"],
                    "height": i["height"],
                    "date_captured": i.get("date_captured", ""),
                    "license": 1,
                    "extra": i.get("extra", {}),
                } for i in images
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
                } for a in annotations
            ],
        }

    # ---------------- Annotations queries ----------------
    @staticmethod
    async def get_annotations(
        dataset_name: str,
        user_id: str,
        image_id: Optional[int] = None,
        category_id: Optional[int] = None,
        limit: int = 500,
        skip: int = 0,
    ) -> List[Dict[str, Any]]:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return []
        query: Dict[str, Any] = {"user_id": user_id, "dataset_id": meta["_id"]}
        if image_id is not None:
            query["image_id"] = image_id
        if category_id is not None:
            query["category_id"] = category_id

        cursor = get_collection("annotations").find(query).skip(skip).limit(limit)
        anns = await cursor.to_list(length=None)
        return [
            {
                "id": a["annotation_id"],
                "image_id": a["image_id"],
                "category_id": a["category_id"],
                "bbox": a["bbox"],
                "area": a["area"],
                "segmentation": a.get("segmentation", []),
                "iscrowd": a.get("iscrowd", 0),
            } for a in anns
        ]

    @staticmethod
    async def get_annotations_count(
        dataset_name: str,
        user_id: str,
        image_id: Optional[int] = None,
        category_id: Optional[int] = None,
    ) -> int:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return 0
        query: Dict[str, Any] = {"user_id": user_id, "dataset_id": meta["_id"]}
        if image_id is not None:
            query["image_id"] = image_id
        if category_id is not None:
            query["category_id"] = category_id
        return await get_collection("annotations").count_documents(query)

    # ------------------------------------------------------------------
    # Category CRUD (Roboflow-style: create classes inside the workspace)
    # ------------------------------------------------------------------
    @staticmethod
    async def create_category(
        dataset_name: str,
        user_id: str,
        name: str,
        supercategory: str = "object",
    ) -> Dict[str, Any]:
        """Create a new category in a dataset (creates the dataset on demand).

        Returns the inserted category document (with assigned ``id``).
        Raises ``ValueError`` if a category with the same name already exists.
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("Category name is required")

        now = datetime.utcnow()
        meta = await _get_metadata(dataset_name, user_id)
        if meta:
            dataset_id = meta["_id"]
        else:
            dataset_id = str(ObjectId())
            await get_collection("dataset_metadata").insert_one({
                "_id": dataset_id,
                "user_id": user_id,
                "dataset_name": dataset_name,
                "total_categories": 0,
                "total_images": 0,
                "total_annotations": 0,
                "uploaded_at": now,
                "updated_at": now,
                "is_active": True,
            })

        existing = await get_collection("categories").find_one({
            "user_id": user_id,
            "dataset_id": dataset_id,
            "name": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
        })
        if existing:
            raise ValueError(f"Category '{name}' already exists")

        last = await (
            get_collection("categories")
            .find({"user_id": user_id, "dataset_id": dataset_id})
            .sort("category_id", -1)
            .limit(1)
            .to_list(length=1)
        )
        next_id = (last[0]["category_id"] + 1) if last else 1

        await get_collection("categories").insert_one({
            "user_id": user_id,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "category_id": next_id,
            "name": name,
            "supercategory": supercategory or "object",
            "created_at": now,
        })

        await get_collection("dataset_metadata").update_one(
            {"_id": dataset_id},
            {"$set": {"updated_at": now}, "$inc": {"total_categories": 1}},
        )

        logger.success(
            f"[annotation_service] category '{name}' (id={next_id}) created in "
            f"dataset '{dataset_name}'"
        )
        return {
            "id": next_id,
            "name": name,
            "supercategory": supercategory or "object",
            "annotation_count": 0,
        }

    @staticmethod
    async def update_category(
        dataset_name: str,
        user_id: str,
        category_id: int,
        name: Optional[str] = None,
        supercategory: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return None
        update: Dict[str, Any] = {}
        if name:
            update["name"] = name.strip()
        if supercategory is not None:
            update["supercategory"] = supercategory
        if not update:
            return None
        result = await get_collection("categories").update_one(
            {"user_id": user_id, "dataset_id": meta["_id"], "category_id": category_id},
            {"$set": update},
        )
        if result.matched_count == 0:
            return None
        return {"id": category_id, **update}

    @staticmethod
    async def delete_category(
        dataset_name: str,
        user_id: str,
        category_id: int,
    ) -> bool:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return False
        result = await get_collection("categories").delete_one(
            {"user_id": user_id, "dataset_id": meta["_id"], "category_id": category_id}
        )
        if result.deleted_count == 0:
            return False
        # Cascade: delete annotations referencing this category.
        await get_collection("annotations").delete_many({
            "user_id": user_id,
            "dataset_id": meta["_id"],
            "category_id": category_id,
        })
        await get_collection("dataset_metadata").update_one(
            {"_id": meta["_id"]},
            {"$set": {"updated_at": datetime.utcnow()}, "$inc": {"total_categories": -1}},
        )
        return True

    @staticmethod
    async def get_categories_with_stats(dataset_name: str, user_id: str) -> List[Dict[str, Any]]:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return []
        dataset_id = meta["_id"]

        cats = await get_collection("categories").find(
            {"user_id": user_id, "dataset_id": dataset_id}
        ).to_list(length=None)

        # annotation counts per category
        pipeline = [
            {"$match": {"user_id": user_id, "dataset_id": dataset_id}},
            {"$group": {"_id": "$category_id", "count": {"$sum": 1}}},
        ]
        counts = {
            d["_id"]: d["count"]
            async for d in get_collection("annotations").aggregate(pipeline)
        }

        # deterministic colors
        palette = [
            "#FF3B30", "#FF9500", "#FFCC00", "#34C759", "#00C7BE",
            "#30B0C7", "#007AFF", "#5856D6", "#AF52DE", "#FF2D55",
        ]
        result = []
        for idx, c in enumerate(cats):
            result.append({
                "id": c["category_id"],
                "name": c["name"],
                "supercategory": c.get("supercategory", ""),
                "color": palette[idx % len(palette)],
                "annotation_count": counts.get(c["category_id"], 0),
            })
        return result

    # ---------------- Image with annotations ----------------
    @staticmethod
    async def get_image_with_annotations(
        dataset_name: str,
        image_id: int,
        user_id: str,
        include_svg: bool = False,
    ) -> Optional[Dict[str, Any]]:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return None
        dataset_id = meta["_id"]

        image = await get_collection("images").find_one(
            {"user_id": user_id, "dataset_id": dataset_id, "image_id": image_id}
        )
        if not image:
            return None

        anns = await get_collection("annotations").find(
            {"user_id": user_id, "dataset_id": dataset_id, "image_id": image_id}
        ).to_list(length=None)

        cats = await get_collection("categories").find(
            {"user_id": user_id, "dataset_id": dataset_id}
        ).to_list(length=None)
        cat_map = {c["category_id"]: c["name"] for c in cats}

        formatted = [
            {
                "id": a["annotation_id"],
                "image_id": a["image_id"],
                "bbox": a["bbox"],
                "category_id": a["category_id"],
                "category_name": cat_map.get(a["category_id"], "unknown"),
                "area": a["area"],
                "segmentation": a.get("segmentation", []),
                "iscrowd": a.get("iscrowd", 0),
            } for a in anns
        ]

        return {
            "image": {
                "id": image["image_id"],
                "file_name": image["file_name"],
                "stored_filename": image.get("stored_filename"),
                "image_url": image.get("image_url") or _build_image_url(image.get("stored_filename")),
                "width": image["width"],
                "height": image["height"],
                "date_captured": image.get("date_captured", ""),
                "extra": image.get("extra", {}),
            },
            "annotations": formatted,
            "total_annotations": len(formatted),
            "dataset_name": dataset_name,
        }

    # Backwards-compat alias used by some tests/code
    @staticmethod
    async def get_image_annotations(dataset_name: str, image_id: int, user_id: str) -> Dict[str, Any]:
        result = await AnnotationService.get_image_with_annotations(dataset_name, image_id, user_id)
        return result or {"error": "Not found"}

    # ---------------- Annotation create / update / delete ----------------
    @staticmethod
    async def add_annotation(dataset_name: str, user_id: str, annotation: Annotation,
                              annotator_id: Optional[str] = None,
                              batch_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return None
        dataset_id = meta["_id"]
        anns_col = get_collection("annotations")

        # If id collides or is a placeholder, allocate the next free id
        existing = await anns_col.find_one(
            {"user_id": user_id, "dataset_id": dataset_id, "annotation_id": annotation.id}
        )
        ann_id = annotation.id
        if existing:
            last = await anns_col.find_one(
                {"user_id": user_id, "dataset_id": dataset_id}, sort=[("annotation_id", -1)]
            )
            ann_id = (last["annotation_id"] + 1) if last else 1

        now = datetime.utcnow()
        doc: Dict[str, Any] = {
            "user_id": user_id,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "annotation_id": ann_id,
            "image_id": annotation.image_id,
            "category_id": annotation.category_id,
            "bbox": annotation.bbox,
            "area": annotation.area,
            "segmentation": annotation.segmentation,
            "iscrowd": annotation.iscrowd,
            "uploaded_at": now,
            "updated_at": now,
        }
        if annotator_id:
            doc["annotator_id"] = annotator_id
        if batch_id:
            doc["batch_id"] = batch_id
        await anns_col.insert_one(doc)

        total = await anns_col.count_documents({"user_id": user_id, "dataset_id": dataset_id})
        await get_collection("dataset_metadata").update_one(
            {"_id": dataset_id},
            {"$set": {"total_annotations": total, "updated_at": now}},
        )
        return {"annotation_id": ann_id, "dataset_name": dataset_name}

    @staticmethod
    async def add_or_update_annotation(dataset_name: str, user_id: str, annotation: Annotation) -> Dict[str, Any]:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return {"success": False, "error": "Dataset not found"}
        dataset_id = meta["_id"]
        anns_col = get_collection("annotations")
        existing = await anns_col.find_one(
            {"user_id": user_id, "dataset_id": dataset_id, "annotation_id": annotation.id}
        )
        now = datetime.utcnow()
        if existing:
            await anns_col.update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "bbox": annotation.bbox,
                    "area": annotation.area,
                    "category_id": annotation.category_id,
                    "segmentation": annotation.segmentation,
                    "iscrowd": annotation.iscrowd,
                    "updated_at": now,
                }},
            )
            return {"success": True, "message": f"Annotation {annotation.id} updated"}
        result = await AnnotationService.add_annotation(dataset_name, user_id, annotation)
        return {"success": True, "message": f"Annotation {result['annotation_id']} added"}

    @staticmethod
    async def batch_update_annotations(
        dataset_name: str,
        user_id: str,
        annotations: List[Annotation],
    ) -> int:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return 0
        dataset_id = meta["_id"]
        anns_col = get_collection("annotations")
        now = datetime.utcnow()
        updated = 0
        for ann in annotations:
            res = await anns_col.update_one(
                {"user_id": user_id, "dataset_id": dataset_id, "annotation_id": ann.id},
                {"$set": {
                    "bbox": ann.bbox,
                    "area": ann.area,
                    "category_id": ann.category_id,
                    "segmentation": ann.segmentation,
                    "iscrowd": ann.iscrowd,
                    "updated_at": now,
                }},
            )
            if res.modified_count > 0:
                updated += 1
        await get_collection("dataset_metadata").update_one(
            {"_id": dataset_id}, {"$set": {"updated_at": now}}
        )
        return updated

    @staticmethod
    async def delete_annotation(dataset_name: str, user_id: str, annotation_id: int) -> bool:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return False
        dataset_id = meta["_id"]
        result = await get_collection("annotations").delete_one(
            {"user_id": user_id, "dataset_id": dataset_id, "annotation_id": annotation_id}
        )
        if result.deleted_count > 0:
            total = await get_collection("annotations").count_documents(
                {"user_id": user_id, "dataset_id": dataset_id}
            )
            await get_collection("dataset_metadata").update_one(
                {"_id": dataset_id},
                {"$set": {"total_annotations": total, "updated_at": datetime.utcnow()}},
            )
            return True
        return False

    # @staticmethod
    # async def delete_image_annotations(dataset_name: str, user_id: str, image_id: int) -> int:
    #     meta = await _get_metadata(dataset_name, user_id)
    #     if not meta:
    #         return 0
    #     dataset_id = meta["_id"]
    #     result = await get_collection("annotations").delete_many(
    #         {"user_id": user_id, "dataset_id": dataset_id, "image_id": image_id}
    #     )
    #     if result.deleted_count > 0:
    #         total = await get_collection("annotations").count_documents(
    #             {"user_id": user_id, "dataset_id": dataset_id}
    #         )
    #         await get_collection("dataset_metadata").update_one(
    #             {"_id": dataset_id},
    #             {"$set": {"total_annotations": total, "updated_at": datetime.utcnow()}},
    #         )
    #     return result.deleted_count

    @staticmethod
    async def delete_image(dataset_name: str, user_id: str, image_id: int) -> bool:
        """Delete an image and all its associated annotations."""
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return False
        dataset_id = meta["_id"]
        
        # Delete all annotations for this image
        await get_collection("annotations").delete_many(
            {"user_id": user_id, "dataset_id": dataset_id, "image_id": image_id}
        )
        
        # Delete the image record
        result = await get_collection("images").delete_one(
            {"user_id": user_id, "dataset_id": dataset_id, "image_id": image_id}
        )
        
        if result.deleted_count > 0:
            # Update image count
            total_images = await get_collection("images").count_documents(
                {"user_id": user_id, "dataset_id": dataset_id}
            )
            # Update annotation count
            total_annotations = await get_collection("annotations").count_documents(
                {"user_id": user_id, "dataset_id": dataset_id}
            )
            await get_collection("dataset_metadata").update_one(
                {"_id": dataset_id},
                {"$set": {
                    "total_images": total_images,
                    "total_annotations": total_annotations,
                    "updated_at": datetime.utcnow()
                }},
            )
            return True
        return False

    # ---------------- Dataset deletion ----------------
    @staticmethod
    async def delete_dataset(dataset_name: str, user_id: str, hard_delete: bool = False) -> bool:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return False
        dataset_id = meta["_id"]
        if hard_delete:
            await get_collection("annotations").delete_many({"dataset_id": dataset_id})
            await get_collection("images").delete_many({"dataset_id": dataset_id})
            await get_collection("categories").delete_many({"dataset_id": dataset_id})
            await get_collection("dataset_metadata").delete_one({"_id": dataset_id})
            await get_collection("task_batches").delete_many({"$or":[{"dataset_id": dataset_id}, {"dataset_name": dataset_name}]})
        else:
            # Soft delete: mark dataset inactive and clean up associated task batches
            await get_collection("dataset_metadata").update_one(
                {"_id": dataset_id},
                {"$set": {"is_active": False, "updated_at": datetime.utcnow()}},
            )
            # Clean up task batches associated with this dataset
            await get_collection("task_batches").delete_many(
                {"$or": [{"dataset_id": dataset_id}, {"project_id": dataset_name}]}
            )
        return True

    # ---------------- Statistics ----------------
    @staticmethod
    async def get_dataset_statistics(dataset_name: str, user_id: str) -> Optional[Dict[str, Any]]:
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return None
        dataset_id = meta["_id"]

        total_annotations = await get_collection("annotations").count_documents(
            {"user_id": user_id, "dataset_id": dataset_id}
        )
        total_images = await get_collection("images").count_documents(
            {"user_id": user_id, "dataset_id": dataset_id}
        )

        # Distinct count of images that have at least one annotation
        annotated_image_ids = await get_collection("annotations").distinct(
            "image_id",
            {"user_id": user_id, "dataset_id": dataset_id},
        )
        annotated_images = len(annotated_image_ids)

        pipeline = [
            {"$match": {"user_id": user_id, "dataset_id": dataset_id}},
            {"$group": {"_id": "$category_id", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
        dist = await get_collection("annotations").aggregate(pipeline).to_list(length=None)
        cats = await get_collection("categories").find(
            {"user_id": user_id, "dataset_id": dataset_id}
        ).to_list(length=None)
        cat_map = {c["category_id"]: c["name"] for c in cats}

        return {
            "dataset_name": dataset_name,
            "total_images": total_images,
            "annotated_images": annotated_images,
            "unannotated_images": max(0, total_images - annotated_images),
            "total_annotations": total_annotations,
            "total_categories": meta.get("total_categories", 0),
            "annotations_per_image": (
                total_annotations / total_images if total_images > 0 else 0
            ),
            "category_distribution": [
                {
                    "category_id": d["_id"],
                    "category_name": cat_map.get(d["_id"], "unknown"),
                    "annotation_count": d["count"],
                } for d in dist
            ],
            "uploaded_at": meta["uploaded_at"].isoformat() if meta.get("uploaded_at") else None,
            "last_updated": meta["updated_at"].isoformat() if meta.get("updated_at") else None,
        }


    # ---------------- Paginated images with page-based pagination (for UI gallery) ----------------
    @staticmethod
    async def get_paginated_images(
        dataset_name: str,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        search: Optional[str] = None,
        annotation_status: Optional[str] = None,
        include_svg: bool = False,
        image_id_min: Optional[int] = None,
        image_id_max: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get paginated images with page-based pagination for the UI gallery.
        
        Args:
            dataset_name: Name of the dataset
            user_id: User ID
            page: Page number (1-indexed)
            page_size: Number of images per page (default 20)
            search: Filter by filename (case-insensitive substring match)
            annotation_status: 'annotated' | 'not_annotated' | None
            include_svg: Whether to generate SVG preview
        
        Returns:
            Dictionary with images list and pagination info
        """
        # Get dataset metadata
        meta = await _get_metadata(dataset_name, user_id)
        if not meta:
            return None
        dataset_id = meta["_id"]
        
        # Build base query for images
        query: Dict[str, Any] = {"user_id": user_id, "dataset_id": dataset_id}
        
        # Apply filename search filter
        if search:
            query["file_name"] = {"$regex": search, "$options": "i"}

        # Apply batch image_id range filter (for batch-mode access)
        if image_id_min is not None or image_id_max is not None:
            range_q: Dict[str, Any] = {}
            if image_id_min is not None:
                range_q["$gte"] = image_id_min
            if image_id_max is not None:
                range_q["$lte"] = image_id_max
            # Merge with any existing image_id filter from annotation_status below
            query["_batch_range"] = range_q  # temp key, resolved below
        
        # If filtering by annotation status, first get the set of annotated image IDs
        annotated_image_ids = None
        if annotation_status in ("annotated", "not_annotated"):
            base_q = {"user_id": user_id, "dataset_id": dataset_id}
            annotated_image_ids = set(
                await get_collection("annotations").distinct(
                    "image_id",
                    base_q,
                )
            )
            status_ids = list(annotated_image_ids) if annotation_status == "annotated" else None
            not_ids = list(annotated_image_ids) if annotation_status == "not_annotated" else None
        else:
            status_ids = None
            not_ids = None

        # Resolve image_id filters (range + annotation status)
        batch_range = query.pop("_batch_range", None)
        id_conditions = []
        if batch_range:
            id_conditions.append({"image_id": batch_range})
        if status_ids is not None:
            id_conditions.append({"image_id": {"$in": status_ids}})
        if not_ids is not None:
            id_conditions.append({"image_id": {"$nin": not_ids}})
        if len(id_conditions) == 1:
            query["image_id"] = id_conditions[0]["image_id"]
        elif len(id_conditions) > 1:
            query["$and"] = id_conditions
        
        # Get total count for pagination metadata
        total_images = await get_collection("images").count_documents(query)
        
        # Calculate pagination values
        import math
        total_pages = max(1, math.ceil(total_images / page_size))
        # Clamp page to valid range
        page = max(1, min(page, total_pages))
        skip = (page - 1) * page_size
        
        logger.info(f"📄 Pagination: total_images={total_images}, page_size={page_size}, total_pages={total_pages}, current_page={page}, skip={skip}")
        
        # Fetch the images for this page
        images = await (
            get_collection("images")
            .find(query)
            .sort("image_id", 1)
            .skip(skip)
            .limit(page_size)
            .to_list(length=page_size)
        )
        
        logger.info(f"✅ Fetched {len(images)} images for page {page}")
        
        # If no images found, return empty result with pagination info
        if not images:
            return {
                "images": [],
                "pagination": {
                    "total_images": total_images,
                    "total_pages": total_pages,
                    "current_page": page,
                    "page_size": page_size,
                    "has_next": False,
                    "has_previous": page > 1,
                },
            }
        
        # Batch fetch annotation counts for all images on this page
        image_ids = [img["image_id"] for img in images]
        
        # Get annotation counts per image via aggregation
        ann_count_pipeline = [
            {"$match": {"user_id": user_id, "dataset_id": dataset_id, "image_id": {"$in": image_ids}}},
            {"$group": {"_id": "$image_id", "count": {"$sum": 1}}},
        ]
        ann_counts = {
            d["_id"]: d["count"]
            async for d in get_collection("annotations").aggregate(ann_count_pipeline)
        }
        
        # Get categories for category_name lookup (only if include_svg or we need annotations)
        cats = await get_collection("categories").find(
            {"user_id": user_id, "dataset_id": dataset_id}
        ).to_list(length=None)
        cat_map = {c["category_id"]: c["name"] for c in cats}
        
        # Optionally batch-fetch full annotations (only if include_svg is True)
        anns_by_image: Dict[int, List] = {}
        if include_svg:
            annotations = await get_collection("annotations").find(
                {"user_id": user_id, "dataset_id": dataset_id, "image_id": {"$in": image_ids}}
            ).to_list(length=None)
            for ann in annotations:
                anns_by_image.setdefault(ann["image_id"], []).append(ann)
        
        # Format the response
        formatted_images = []
        for image in images:
            img_id = image["image_id"]
            ann_count = ann_counts.get(img_id, 0)
            
            # Build image object
            image_obj = {
                "id": img_id,
                "image_id": img_id,
                "file_name": image["file_name"],
                "stored_filename": image.get("stored_filename"),
                "image_url": image.get("image_url") or _build_image_url(image.get("stored_filename")),
                "thumbnail_url": _build_thumbnail_url(image.get("stored_filename")),
                "width": image["width"],
                "height": image["height"],
                "date_captured": image.get("date_captured", ""),
                "annotation_count": ann_count,
                "is_annotated": ann_count > 0,
                "extra": image.get("extra", {}),
            }
            
            # Optionally generate SVG preview
            if include_svg and img_id in anns_by_image:
                image_obj["svg_preview"] = await AnnotationService._generate_svg_preview(
                    image["width"], image["height"], anns_by_image[img_id], cat_map
                )
            
            formatted_images.append(image_obj)
        
        return {
            "images": formatted_images,
            "pagination": {
                "total_images": total_images,
                "total_pages": total_pages,
                "current_page": page,
                "page_size": page_size,
                "has_next": page < total_pages,
                "has_previous": page > 1,
            },
        }

    @staticmethod
    async def _generate_svg_preview(width: int, height: int, annotations: List[Dict], cat_map: Dict) -> str:
        """Generate a simple SVG preview of bounding boxes (for thumbnail view)."""
        # Limit to max 10 boxes for performance
        annotations = annotations[:10]
        
        svg_parts = [
            f'<svg width="{min(width, 400)}" height="{min(height, 400)}" '
            f'viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">',
            f'<rect width="{width}" height="{height}" fill="#f8f9fa"/>'
        ]
        
        for ann in annotations:
            bbox = ann["bbox"]
            if len(bbox) >= 4:
                x, y, w, h = bbox[:4]
                category_name = cat_map.get(ann["category_id"], "unknown")
                svg_parts.append(
                    f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
                    f'stroke="#FF4444" stroke-width="2" fill="none" stroke-dasharray="4"/>'
                )
                svg_parts.append(
                    f'<text x="{x}" y="{y-5}" fill="#FF4444" font-size="12" font-family="Arial">'
                    f'{category_name}</text>'
                )
        
        svg_parts.append('</svg>')
        return "".join(svg_parts)