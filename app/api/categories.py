"""
Categories API — manual class management for interactive annotation.

These endpoints let the frontend create, rename, recolour, and delete
custom classes (categories) on an existing dataset — supporting the
"draw a bbox, pick or create a class" Roboflow-style workflow.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from app.auth.dependencies import get_current_active_user
from app.config.database import get_collection
from app.schemas.response import ResponseModel

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    supercategory: str = Field(default="object", max_length=120)


class CategoryUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    supercategory: Optional[str] = Field(None, max_length=120)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _resolve_dataset(dataset_name: str, user_id: str) -> dict:
    meta = await get_collection("dataset_metadata").find_one(
        {"dataset_name": dataset_name, "user_id": user_id, "is_active": True}
    )
    if not meta:
        raise HTTPException(status_code=404, detail=f"Dataset '{dataset_name}' not found")
    return meta


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/{dataset_name}/categories", response_model=ResponseModel)
async def create_category(
    dataset_name: str,
    payload: CategoryCreate,
    current_user: dict = Depends(get_current_active_user),
):
    """Create a new class/category on an existing dataset."""
    user_id = current_user["_id"]
    meta = await _resolve_dataset(dataset_name, user_id)
    dataset_id = meta["_id"]

    cats_col = get_collection("categories")
    if await cats_col.find_one({
        "user_id": user_id, "dataset_id": dataset_id, "name": payload.name,
    }):
        raise HTTPException(status_code=409, detail=f"Class '{payload.name}' already exists")

    last = await cats_col.find_one(
        {"user_id": user_id, "dataset_id": dataset_id},
        sort=[("category_id", -1)],
    )
    next_id = (last["category_id"] + 1) if last else 1
    now = datetime.utcnow()

    await cats_col.insert_one({
        "user_id": user_id,
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "category_id": next_id,
        "name": payload.name,
        "supercategory": payload.supercategory,
        "created_at": now,
    })

    total = await cats_col.count_documents(
        {"user_id": user_id, "dataset_id": dataset_id}
    )
    await get_collection("dataset_metadata").update_one(
        {"_id": dataset_id},
        {"$set": {"total_categories": total, "updated_at": now}},
    )

    logger.info(f"[categories] +{payload.name} (id={next_id}) on {dataset_name}")
    return ResponseModel(
        success=True,
        message="Category created",
        data={"id": next_id, "name": payload.name, "supercategory": payload.supercategory},
    )


@router.put("/{dataset_name}/categories/{category_id}", response_model=ResponseModel)
async def update_category(
    dataset_name: str,
    category_id: int,
    payload: CategoryUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    """Rename a class or change its supercategory."""
    user_id = current_user["_id"]
    meta = await _resolve_dataset(dataset_name, user_id)

    update: dict = {}
    if payload.name is not None:
        update["name"] = payload.name
    if payload.supercategory is not None:
        update["supercategory"] = payload.supercategory
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")

    res = await get_collection("categories").update_one(
        {"user_id": user_id, "dataset_id": meta["_id"], "category_id": category_id},
        {"$set": update},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail=f"Category {category_id} not found")

    await get_collection("dataset_metadata").update_one(
        {"_id": meta["_id"]}, {"$set": {"updated_at": datetime.utcnow()}}
    )
    return ResponseModel(success=True, message="Category updated", data={"id": category_id, **update})


@router.delete("/{dataset_name}/categories/{category_id}", response_model=ResponseModel)
async def delete_category(
    dataset_name: str,
    category_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    """Delete a class and **all** annotations referencing it."""
    user_id = current_user["_id"]
    meta = await _resolve_dataset(dataset_name, user_id)
    dataset_id = meta["_id"]

    cat = await get_collection("categories").find_one(
        {"user_id": user_id, "dataset_id": dataset_id, "category_id": category_id}
    )
    if not cat:
        raise HTTPException(status_code=404, detail=f"Category {category_id} not found")

    ann_del = await get_collection("annotations").delete_many(
        {"user_id": user_id, "dataset_id": dataset_id, "category_id": category_id}
    )
    await get_collection("categories").delete_one({"_id": cat["_id"]})

    now = datetime.utcnow()
    cat_total = await get_collection("categories").count_documents(
        {"user_id": user_id, "dataset_id": dataset_id}
    )
    ann_total = await get_collection("annotations").count_documents(
        {"user_id": user_id, "dataset_id": dataset_id}
    )
    await get_collection("dataset_metadata").update_one(
        {"_id": dataset_id},
        {"$set": {
            "total_categories": cat_total,
            "total_annotations": ann_total,
            "updated_at": now,
        }},
    )

    return ResponseModel(
        success=True,
        message=f"Category {category_id} deleted",
        data={"deleted_category_id": category_id, "deleted_annotations": ann_del.deleted_count},
    )
