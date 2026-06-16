"""Dataset management API — list datasets, auto-batch creation, batch assignment."""

from datetime import datetime
from math import ceil
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger

from app.auth.dependencies import get_current_active_user
from app.auth.rbac_dependencies import require_admin
from app.config.database import db_manager, get_collection
from app.repositories import TaskBatchRepository, AuditLogRepository, UserRepository
from app.schemas.rbac import TaskStatus
from app.services.annotation_service import AnnotationService
from app.api.task_batches import _format_batch_with_progress_real

router = APIRouter(prefix="/api/datasets", tags=["Datasets"])


# ─── helpers ─────────────────────────────────────────────────────────────────


async def _serialize_dataset(meta: dict, batch_summary: Optional[dict] = None) -> dict:
    """Convert a dataset_metadata document to a JSON-safe dict."""
    return {
        "id": str(meta["_id"]),
        "dataset_name": meta.get("dataset_name"),
        "total_images": meta.get("total_images", 0),
        "total_annotations": meta.get("total_annotations", 0),
        "uploaded_by": str(meta.get("user_id", "")),
        "uploaded_at": meta.get("uploaded_at").isoformat() if meta.get("uploaded_at") else None,
        "updated_at": meta.get("updated_at").isoformat() if meta.get("updated_at") else None,
        "is_active": meta.get("is_active", True),
        "batch_summary": batch_summary,
    }


async def _get_batch_summary(project_id: str, db) -> dict:
    """Quick batch stats for a single dataset."""
    batch_repo = TaskBatchRepository(db)
    batches, total = await batch_repo.get_batches_by_project(project_id, page=1, page_size=1000)
    if total == 0:
        return {"total": 0, "pending": 0, "assigned": 0, "in_progress": 0, "submitted": 0, "approved": 0, "rework": 0}
    counts: dict = {s.value: 0 for s in TaskStatus}
    for b in batches:
        s = b.get("status", TaskStatus.PENDING.value)
        if s in ("annotated", "under_review"):
            s = TaskStatus.SUBMITTED.value
        elif s == "rejected":
            s = TaskStatus.REWORK.value
        counts[s] = counts.get(s, 0) + 1
    return {
        "total": total,
        "pending": counts.get(TaskStatus.PENDING.value, 0),
        "assigned": counts.get(TaskStatus.ASSIGNED.value, 0),
        "in_progress": counts.get(TaskStatus.IN_PROGRESS.value, 0),
        "submitted": counts.get(TaskStatus.SUBMITTED.value, 0),
        "approved": counts.get(TaskStatus.APPROVED.value, 0),
        "rework": counts.get(TaskStatus.REWORK.value, 0),
    }


# ─── endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=dict)
async def list_datasets(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_active_user),
):
    """
    List datasets.
    - Admin: all datasets across all users.
    - Annotator/Checker: only datasets they have assigned batches in.
    """
    db = db_manager.get_db()
    meta_col = get_collection("dataset_metadata")
    user_role = current_user.get("role", "annotator")
    user_id = str(current_user["_id"])

    if user_role == "admin":
        query: dict = {"is_active": True}
    else:
        # Find datasets the user has been assigned batches in
        assigned_batches = await db["task_batches"].find(
            {"assigned_to": user_id}
        ).to_list(None)
        project_ids = list({b["project_id"] for b in assigned_batches})
        query = {"dataset_name": {"$in": project_ids}, "is_active": True}

    if search:
        query["dataset_name"] = {"$regex": search, "$options": "i"}

    total = await meta_col.count_documents(query)
    skip = (page - 1) * page_size
    docs = await meta_col.find(query).sort("uploaded_at", -1).skip(skip).limit(page_size).to_list(None)

    datasets = []
    for doc in docs:
        summary = await _get_batch_summary(doc["dataset_name"], db)
        datasets.append(await _serialize_dataset(doc, summary))

    return {
        "success": True,
        "data": {
            "datasets": datasets,
            "pagination": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": ceil(total / page_size) if total else 1,
            },
        },
    }


@router.get("/{dataset_name}", response_model=dict)
async def get_dataset(
    dataset_name: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Get dataset details with batch summary."""
    db = db_manager.get_db()
    meta_col = get_collection("dataset_metadata")
    user_role = current_user.get("role", "annotator")
    user_id = str(current_user["_id"])

    doc = await meta_col.find_one({"dataset_name": dataset_name, "is_active": True})
    if not doc:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Access check for non-admin
    if user_role != "admin":
        assigned = await db["task_batches"].find_one(
            {"project_id": dataset_name, "assigned_to": user_id}
        )
        if not assigned:
            raise HTTPException(status_code=403, detail="Access denied")

    summary = await _get_batch_summary(dataset_name, db)
    return {"success": True, "data": await _serialize_dataset(doc, summary)}


@router.get("/{dataset_name}/batches", response_model=dict)
async def list_dataset_batches(
    dataset_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    batch_status: Optional[str] = Query(None, alias="status"),
    current_user: dict = Depends(get_current_active_user),
):
    """List all batches for a dataset. Admin sees all; annotator sees own."""
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    user_repo = UserRepository(db)
    user_role = current_user.get("role", "annotator")
    user_id = str(current_user["_id"])

    status_filter: Optional[TaskStatus] = None
    if batch_status:
        try:
            status_filter = TaskStatus(batch_status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {batch_status}")

    batches, total = await batch_repo.get_batches_by_project(
        dataset_name, page=page, page_size=page_size, status=status_filter
    )

    # For non-admin, only show their own batches
    if user_role not in ("admin", "checker"):
        batches = [b for b in batches if b.get("assigned_to") == user_id]
        total = len(batches)

    # Fetch progress stats for all batches
    import asyncio
    progress_results = await asyncio.gather(*[_format_batch_with_progress_real(b) for b in batches])

    # Enrich with annotator names
    result = []
    user_cache: dict = {}
    for idx, b in enumerate(batches):
        assigned_to = b.get("assigned_to")
        annotator_name = None
        if assigned_to:
            if assigned_to not in user_cache:
                u = await user_repo.get_user_by_id(assigned_to)
                user_cache[assigned_to] = u.get("full_name", "Unknown") if u else "Unknown"
            annotator_name = user_cache[assigned_to]
        
        prog = progress_results[idx]
        
        result.append({
            "id": str(b["_id"]),
            "project_id": b["project_id"],
            "batch_number": b.get("batch_number"),
            "start_index": b.get("start_index"),
            "end_index": b.get("end_index"),
            "image_count": b.get("image_count"),
            "status": b.get("status"),
            "assigned_to": assigned_to,
            "annotator_name": annotator_name,
            "deadline": b.get("deadline").isoformat() if b.get("deadline") else None,
            "submitted_at": b.get("submitted_at").isoformat() if b.get("submitted_at") else None,
            "reviewed_at": b.get("reviewed_at").isoformat() if b.get("reviewed_at") else None,
            "rejection_reason": b.get("rejection_reason"),
            "created_at": b.get("created_at").isoformat() if b.get("created_at") else None,
            # Progress counts
            "images_annotated": prog.images_annotated,
            "annotated_count": prog.images_annotated,       # for BatchesPage.jsx
            "completed_images": prog.images_annotated,      # for BatchesPage.jsx
            "images_pending": prog.images_pending,
            "progress_percentage": prog.progress_percentage,
        })

    return {
        "success": True,
        "data": {
            "batches": result,
            "pagination": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": ceil(total / page_size) if total else 1,
            },
        },
    }


@router.post("/{dataset_name}/batches/auto", response_model=dict)
async def auto_create_batches(
    dataset_name: str,
    batch_size: int = Query(100, ge=1, le=10000),
    deadline: Optional[datetime] = Query(None),
    current_user: dict = Depends(require_admin),
):
    """
    Auto-create equal-sized batches for a dataset (admin only).
    Fails with 409 if batches already exist for this dataset.
    """
    db = db_manager.get_db()
    meta_col = get_collection("dataset_metadata")
    batch_repo = TaskBatchRepository(db)
    audit_repo = AuditLogRepository(db)

    doc = await meta_col.find_one({"dataset_name": dataset_name, "is_active": True})
    if not doc:
        raise HTTPException(status_code=404, detail="Dataset not found")

    total_images: int = doc.get("total_images", 0)
    if total_images == 0:
        raise HTTPException(status_code=400, detail="Dataset has no images")

    existing_count = await db["task_batches"].count_documents({"project_id": dataset_name})
    if existing_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Batches already exist for dataset '{dataset_name}' "
                f"({existing_count} batch{'es' if existing_count != 1 else ''}). "
                "Assign existing batches or remove them before creating new ones."
            ),
        )

    # Create new batches
    n_batches = ceil(total_images / batch_size)
    created_ids = []
    for i in range(n_batches):
        start = i * batch_size + 1
        end = min((i + 1) * batch_size, total_images)
        count = end - start + 1
        bid = await batch_repo.create_batch(
            project_id=dataset_name,
            batch_number=i + 1,
            start_index=start,
            end_index=end,
            image_count=count,
            deadline=deadline,
        )
        created_ids.append(bid)

    await audit_repo.log_action(
        user_id=str(current_user["_id"]),
        action="auto_create_batches",
        resource_type="dataset",
        resource_id=dataset_name,
        changes={"batch_size": batch_size, "n_batches": n_batches, "total_images": total_images},
    )

    return {
        "success": True,
        "message": f"Created {n_batches} batches for '{dataset_name}'",
        "data": {"dataset_name": dataset_name, "batches_created": n_batches, "batch_ids": created_ids},
    }


@router.get("/{dataset_name}/annotators", response_model=dict)
async def list_annotators_for_dataset(
    dataset_name: str,
    current_user: dict = Depends(require_admin),
):
    """List all annotator users (for assignment dropdowns)."""
    db = db_manager.get_db()
    users = await db["users"].find(
        {"role": {"$in": ["annotator", "checker"]}, "is_active": True},
        {"_id": 1, "full_name": 1, "email": 1, "role": 1},
    ).to_list(None)
    result = [
        {"id": str(u["_id"]), "full_name": u["full_name"], "email": u["email"], "role": u["role"]}
        for u in users
    ]
    return {"success": True, "data": result}
