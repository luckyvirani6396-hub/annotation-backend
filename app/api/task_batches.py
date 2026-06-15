"""Task batch APIs for managing image batches and assignments."""

from datetime import datetime, timedelta
from math import ceil
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from loguru import logger
from bson import ObjectId

from app.auth.rbac_dependencies import require_admin, require_annotator_or_admin, require_checker_or_admin
from app.auth.dependencies import get_current_active_user
from app.config.database import db_manager, get_collection
from app.repositories import TaskBatchRepository, AuditLogRepository, UserRepository
from app.schemas.rbac import (
    TaskStatus,
    TaskBatchCreate,
    TaskBatchAssign,
    TaskBatchResponse,
    TaskBatchWithProgress,
    BatchSubmitRequest,
    BatchReviewRequest,
)
from app.services.annotation_service import AnnotationService

router = APIRouter(prefix="/api/batches", tags=["Task Batches"])


# ─── batch creation ─────────────────────────────────────────────────────────


@router.post("", response_model=TaskBatchResponse)
async def create_batch(
    request: TaskBatchCreate,
    current_user: dict = Depends(require_admin),
):
    """
    Create a task batch for a project (admin only).
    
    Batches are created automatically when large projects are uploaded,
    but can also be created manually to subdivide existing projects.
    
    Parameters:
    - project_id: ID of the project
    - batch_number: Sequential batch number (e.g., 1, 2, 3...)
    - start_index: Starting image index (0-based)
    - end_index: Ending image index (inclusive, 0-based)
    - image_count: Total images in batch (end_index - start_index + 1)
    - deadline: Optional deadline for completing batch
    """
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    audit_repo = AuditLogRepository(db)

    try:
        # Validate indices
        if request.start_index > request.end_index:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="start_index must be less than or equal to end_index",
            )

        if request.image_count != (request.end_index - request.start_index + 1):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="image_count doesn't match index range",
            )

        # Check if batch number already exists
        existing_batches, _ = await batch_repo.get_batches_by_project(
            request.project_id,
            page=1,
            page_size=1000,
        )
        batch_numbers = [b["batch_number"] for b in existing_batches]
        if request.batch_number in batch_numbers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Batch {request.batch_number} already exists for this project",
            )

        # Create batch
        batch_id = await batch_repo.create_batch(
            project_id=request.project_id,
            batch_number=request.batch_number,
            start_index=request.start_index,
            end_index=request.end_index,
            image_count=request.image_count,
            deadline=request.deadline,
        )

        # Log audit trail
        await audit_repo.log_action(
            user_id=str(current_user["_id"]),
            action="create_batch",
            resource_type="batch",
            resource_id=batch_id,
            changes={
                "project_id": request.project_id,
                "batch_number": request.batch_number,
                "image_count": request.image_count,
            },
        )

        batch = await batch_repo.get_batch_by_id(batch_id)
        return _format_batch_response(batch)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating batch: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create batch",
        )


# ─── batch listing ──────────────────────────────────────────────────────────


@router.get("", response_model=dict)
async def list_batches(
    project_id: str = Query(..., description="Project ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    status: str = Query(None),
    current_user: dict = Depends(require_admin),
):
    """
    List batches for a project (admin only).
    
    Query parameters:
    - project_id: Required - filter by project
    - page: Page number (1-indexed)
    - page_size: Items per page
    - status: Filter by status (pending, assigned, annotated, under_review, approved, rejected)
    """
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    meta_col = get_collection("dataset_metadata")

    try:
        # Verify project exists and is active
        dataset = await meta_col.find_one({"dataset_name": project_id, "is_active": True})
        if not dataset:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_id}' not found or has been deleted"
            )

        # Parse status filter
        status_filter = None
        if status:
            try:
                status_filter = TaskStatus(status)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status: {status}",
                )

        batches, total = await batch_repo.get_batches_by_project(
            project_id=project_id,
            page=page,
            page_size=page_size,
            status=status_filter,
        )

        total_pages = (total + page_size - 1) // page_size

        # TODO: Fetch progress data for each batch
        return {
            "success": True,
            "data": {
                "batches": [_format_batch_response(b) for b in batches],
                "pagination": {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                },
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing batches: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list batches",
        )


@router.get("/all", response_model=dict)
async def list_all_batches(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    status: str = Query(None),
    project_id: str = Query(None, description="Optional dataset name filter"),
    search: str = Query(None, description="Search by dataset name or batch number"),
    current_user: dict = Depends(require_admin),
):
    """
    List all batches across every active dataset (admin only).

    Query parameters:
    - page / page_size: pagination
    - status: filter by batch status
    - project_id: optional dataset name filter
    - search: optional text search on dataset name or batch number
    """
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    user_repo = UserRepository(db)
    meta_col = get_collection("dataset_metadata")

    try:
        status_filter = None
        if status:
            try:
                status_filter = TaskStatus(status).value
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status: {status}",
                )

        active_project_ids = None
        if project_id:
            dataset = await meta_col.find_one({"dataset_name": project_id, "is_active": True})
            if not dataset:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Project '{project_id}' not found or has been deleted",
                )
        else:
            active_datasets = await meta_col.find(
                {"is_active": True},
                {"dataset_name": 1},
            ).to_list(None)
            active_project_ids = [d["dataset_name"] for d in active_datasets]
            if not active_project_ids:
                return {
                    "success": True,
                    "data": {
                        "batches": [],
                        "pagination": {
                            "total": 0,
                            "page": page,
                            "page_size": page_size,
                            "total_pages": 0,
                        },
                    },
                }

        batches, total = await batch_repo.list_all_batches(
            page=page,
            page_size=page_size,
            status=status_filter,
            project_id=project_id,
            project_ids=active_project_ids,
            search=search,
        )

        user_cache: dict = {}
        result = []
        for b in batches:
            assigned_to = b.get("assigned_to")
            annotator_name = None
            if assigned_to:
                if assigned_to not in user_cache:
                    u = await user_repo.get_user_by_id(assigned_to)
                    user_cache[assigned_to] = u.get("full_name", "Unknown") if u else "Unknown"
                annotator_name = user_cache[assigned_to]
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
            })

        total_pages = ceil(total / page_size) if total else 0
        return {
            "success": True,
            "data": {
                "batches": result,
                "pagination": {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                },
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing all batches: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list all batches",
        )


@router.get("/{batch_id}", response_model=TaskBatchWithProgress)
async def get_batch(
    batch_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Get batch details. Admin can see any batch; annotator can see their assigned batch."""
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    meta_col = get_collection("dataset_metadata")

    batch = await batch_repo.get_batch_by_id(batch_id)
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")

    # Verify dataset is still active
    project_id = batch.get("project_id")
    dataset = await meta_col.find_one({"dataset_name": project_id, "is_active": True})
    if not dataset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Associated dataset has been deleted")

    # Access check: admin/checker can see all; annotator only sees their own
    user_role = current_user.get("role", "annotator")
    user_id = str(current_user["_id"])
    if user_role not in ("admin", "checker") and batch.get("assigned_to") != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorised to view this batch")

    return await _format_batch_with_progress_real(batch)


# ─── batch assignment ───────────────────────────────────────────────────────


@router.post("/{batch_id}/assign", response_model=TaskBatchResponse)
async def assign_batch(
    batch_id: str,
    request: TaskBatchAssign,
    current_user: dict = Depends(require_admin),
):
    """
    Assign batch to annotator (admin only).
    
    Transitions batch from PENDING to ASSIGNED status.
    """
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    user_repo = UserRepository(db)
    audit_repo = AuditLogRepository(db)

    try:
        # Get batch
        batch = await batch_repo.get_batch_by_id(batch_id)
        if not batch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Batch not found",
            )

        # Verify annotator exists
        annotator = await user_repo.get_user_by_id(request.annotator_id)
        if not annotator:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Annotator not found",
            )

        # Assign batch
        success = await batch_repo.assign_batch(
            batch_id=batch_id,
            annotator_id=request.annotator_id,
            due_date=request.due_date,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to assign batch",
            )

        # Log audit trail
        await audit_repo.log_action(
            user_id=str(current_user["_id"]),
            action="assign_batch",
            resource_type="batch",
            resource_id=batch_id,
            changes={
                "assigned_to": request.annotator_id,
                "status": TaskStatus.ASSIGNED.value,
            },
        )

        updated_batch = await batch_repo.get_batch_by_id(batch_id)
        return _format_batch_response(updated_batch)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error assigning batch: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to assign batch",
        )


@router.post("/{batch_id}/reassign", response_model=TaskBatchResponse)
async def reassign_batch(
    batch_id: str,
    request: TaskBatchAssign,
    current_user: dict = Depends(require_admin),
):
    """
    Reassign batch to different annotator (admin only).
    
    Can be used to reassign incomplete batches or redistribute work.
    """
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    user_repo = UserRepository(db)
    audit_repo = AuditLogRepository(db)

    try:
        # Get batch
        batch = await batch_repo.get_batch_by_id(batch_id)
        if not batch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Batch not found",
            )

        # Verify new annotator exists
        annotator = await user_repo.get_user_by_id(request.annotator_id)
        if not annotator:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Annotator not found",
            )

        old_annotator = batch.get("assigned_to")

        # Reassign batch
        success = await batch_repo.reassign_batch(
            batch_id=batch_id,
            annotator_id=request.annotator_id,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to reassign batch",
            )

        # Log audit trail
        await audit_repo.log_action(
            user_id=str(current_user["_id"]),
            action="reassign_batch",
            resource_type="batch",
            resource_id=batch_id,
            changes={
                "reassigned_from": old_annotator,
                "reassigned_to": request.annotator_id,
            },
        )

        updated_batch = await batch_repo.get_batch_by_id(batch_id)
        return _format_batch_response(updated_batch)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reassigning batch: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reassign batch",
        )


# ─── annotator-specific endpoints ────────────────────────────────────────────


@router.get("/my-assignments/list", response_model=dict)
async def get_my_assignments(
    status: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(require_annotator_or_admin),
):
    """
    Get current user's assigned batches (annotators and admins).
    
    Admins can see all batches; annotators see only their own.
    Only returns batches from active (not deleted) datasets.
    """
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    meta_col = get_collection("dataset_metadata")

    try:
        # Get user ID from token
        user_id = str(current_user["_id"])

        # Parse status filter
        status_filter = None
        if status:
            try:
                status_filter = TaskStatus(status)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status: {status}",
                )

        # Get batches assigned to this annotator
        batches = await batch_repo.get_batches_by_annotator(
            annotator_id=user_id,
            status=status_filter,
        )

        # Filter out batches from deleted datasets
        active_batches = []
        for batch in batches:
            project_id = batch.get("project_id")
            # Check if dataset is still active
            dataset = await meta_col.find_one(
                {"dataset_name": project_id, "is_active": True}
            )
            if dataset:
                active_batches.append(batch)

        # Apply pagination
        total = len(active_batches)
        skip = (page - 1) * page_size
        paginated = active_batches[skip:skip + page_size]
        total_pages = (total + page_size - 1) // page_size

        return {
            "success": True,
            "data": {
                "batches": [_format_batch_with_progress(b) for b in paginated],
                "pagination": {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                },
            },
        }

    except Exception as e:
        logger.error(f"Error getting assignments: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get assignments",
        )


# ─── batch images ────────────────────────────────────────────────────────────


@router.get("/{batch_id}/images", response_model=dict)
async def get_batch_images(
    batch_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    annotation_status: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_active_user),
):
    """Get paginated images for a batch (with cross-user access)."""
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    meta_col = get_collection("dataset_metadata")

    batch = await batch_repo.get_batch_by_id(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    # Verify dataset is still active
    project_id = batch.get("project_id")
    dataset = await meta_col.find_one({"dataset_name": project_id, "is_active": True})
    if not dataset:
        raise HTTPException(status_code=404, detail="Associated dataset has been deleted")

    user_role = current_user.get("role", "annotator")
    user_id = str(current_user["_id"])
    if user_role not in ("admin", "checker") and batch.get("assigned_to") != user_id:
        raise HTTPException(status_code=403, detail="Not authorised to access this batch")

    # Use dataset owner's user_id for image access
    dataset_name = batch["project_id"]
    owner_id = await AnnotationService.get_dataset_owner_id(dataset_name)
    if not owner_id:
        raise HTTPException(status_code=404, detail="Dataset not found")

    result = await AnnotationService.get_paginated_images(
        dataset_name=dataset_name,
        user_id=owner_id,
        page=page,
        page_size=page_size,
        search=search,
        annotation_status=annotation_status,
        image_id_min=batch.get("start_index"),
        image_id_max=batch.get("end_index"),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    return {"success": True, "data": result}


# ─── workflow transitions ──────────────────────────────────────────────────


@router.post("/{batch_id}/submit", response_model=dict)
async def submit_batch(
    batch_id: str,
    request: BatchSubmitRequest,
    current_user: dict = Depends(get_current_active_user),
):
    """Annotator submits completed batch for checker review.

    Allowed from: assigned, pending (first pass), or rejected (rework after send-back).
    """
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    audit_repo = AuditLogRepository(db)

    batch = await batch_repo.get_batch_by_id(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    user_id = str(current_user["_id"])
    user_role = current_user.get("role", "annotator")

    # Only the assigned annotator (or admin) can submit
    if user_role != "admin" and batch.get("assigned_to") != user_id:
        raise HTTPException(status_code=403, detail="Only the assigned annotator can submit this batch")

    if batch["status"] not in (
        TaskStatus.ASSIGNED.value,
        TaskStatus.PENDING.value,
        TaskStatus.REJECTED.value,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Batch cannot be submitted from status '{batch['status']}'",
        )

    ok = await batch_repo.update_batch_fields(
        batch_id,
        status=TaskStatus.ANNOTATED.value,
        submitted_at=datetime.utcnow(),
        submission_notes=request.notes,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to submit batch")

    await audit_repo.log_action(
        user_id=user_id,
        action="submit_batch",
        resource_type="batch",
        resource_id=batch_id,
        changes={"status": TaskStatus.ANNOTATED.value, "notes": request.notes},
    )
    return {"success": True, "message": "Batch submitted for review"}


@router.post("/{batch_id}/approve", response_model=dict)
async def approve_batch(
    batch_id: str,
    request: BatchReviewRequest,
    current_user: dict = Depends(require_checker_or_admin),
):
    """Checker/admin approves a batch (annotated/under_review → approved)."""
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    audit_repo = AuditLogRepository(db)

    batch = await batch_repo.get_batch_by_id(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    if batch["status"] not in (TaskStatus.ANNOTATED.value, TaskStatus.UNDER_REVIEW.value):
        raise HTTPException(
            status_code=400,
            detail=f"Batch cannot be approved from status '{batch['status']}'",
        )

    ok = await batch_repo.update_batch_fields(
        batch_id,
        status=TaskStatus.APPROVED.value,
        reviewed_by=str(current_user["_id"]),
        reviewed_at=datetime.utcnow(),
        review_notes=request.notes,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to approve batch")

    await audit_repo.log_action(
        user_id=str(current_user["_id"]),
        action="approve_batch",
        resource_type="batch",
        resource_id=batch_id,
        changes={"status": TaskStatus.APPROVED.value, "notes": request.notes},
    )
    return {"success": True, "message": "Batch approved"}


@router.post("/{batch_id}/reject", response_model=dict)
async def reject_batch(
    batch_id: str,
    request: BatchReviewRequest,
    current_user: dict = Depends(require_checker_or_admin),
):
    """Checker/admin rejects a batch, sending it back to the annotator (→ assigned)."""
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    audit_repo = AuditLogRepository(db)

    batch = await batch_repo.get_batch_by_id(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    if batch["status"] not in (TaskStatus.ANNOTATED.value, TaskStatus.UNDER_REVIEW.value):
        raise HTTPException(
            status_code=400,
            detail=f"Batch cannot be rejected from status '{batch['status']}'",
        )

    if not request.reason:
        raise HTTPException(status_code=400, detail="Rejection reason is required")

    ok = await batch_repo.update_batch_fields(
        batch_id,
        status=TaskStatus.REJECTED.value,  # send back to annotator
        reviewed_by=str(current_user["_id"]),
        reviewed_at=datetime.utcnow(),
        review_notes=request.notes,
        rejection_reason=request.reason,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to reject batch")

    await audit_repo.log_action(
        user_id=str(current_user["_id"]),
        action="reject_batch",
        resource_type="batch",
        resource_id=batch_id,
        changes={"status": TaskStatus.REJECTED.value, "reason": request.reason},
    )
    return {"success": True, "message": "Batch sent back to annotator"}


@router.get("/review/queue", response_model=dict)
async def get_review_queue(
    current_user: dict = Depends(require_checker_or_admin),
):
    """Get all batches ready for review (checker/admin view)."""
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)

    batches = await batch_repo.get_batches_for_review()
    return {
        "success": True,
        "data": {
            "batches": [_format_batch_dict(b) for b in batches],
            "total": len(batches),
        },
    }


# ─── helper functions ───────────────────────────────────────────────────────


def _format_batch_response(batch: dict) -> TaskBatchResponse:
    """Format batch document to response model."""
    return TaskBatchResponse(
        _id=str(batch["_id"]),
        project_id=batch["project_id"],
        batch_number=batch["batch_number"],
        start_index=batch["start_index"],
        end_index=batch["end_index"],
        image_count=batch["image_count"],
        status=TaskStatus(batch["status"]),
        assigned_to=batch.get("assigned_to"),
        assigned_date=batch.get("assigned_date"),
        deadline=batch.get("deadline"),
        created_at=batch.get("created_at"),
        updated_at=batch.get("updated_at"),
    )


def _format_batch_dict(batch: dict) -> dict:
    """Serialize a batch MongoDB document to a plain dict (safe for JSON)."""
    return {
        "id": str(batch["_id"]),
        "project_id": batch["project_id"],
        "batch_number": batch.get("batch_number"),
        "start_index": batch.get("start_index"),
        "end_index": batch.get("end_index"),
        "image_count": batch.get("image_count"),
        "status": batch.get("status"),
        "assigned_to": batch.get("assigned_to"),
        "assigned_date": batch.get("assigned_date").isoformat() if batch.get("assigned_date") else None,
        "deadline": batch.get("deadline").isoformat() if batch.get("deadline") else None,
        "submitted_at": batch.get("submitted_at").isoformat() if batch.get("submitted_at") else None,
        "reviewed_by": batch.get("reviewed_by"),
        "reviewed_at": batch.get("reviewed_at").isoformat() if batch.get("reviewed_at") else None,
        "review_notes": batch.get("review_notes"),
        "rejection_reason": batch.get("rejection_reason"),
        "submission_notes": batch.get("submission_notes"),
        "created_at": batch.get("created_at").isoformat() if batch.get("created_at") else None,
        "updated_at": batch.get("updated_at").isoformat() if batch.get("updated_at") else None,
    }


def _format_batch_with_progress(batch: dict) -> TaskBatchWithProgress:
    """Format batch with progress stats (placeholder counts)."""
    response = _format_batch_response(batch)
    return TaskBatchWithProgress(
        **response.model_dump(),
        images_annotated=0,
        images_pending=batch["image_count"],
        progress_percentage=0.0,
    )


async def _format_batch_with_progress_real(batch: dict) -> TaskBatchWithProgress:
    """Format batch with real annotation progress counts."""
    dataset_name = batch["project_id"]
    owner_id = await AnnotationService.get_dataset_owner_id(dataset_name)
    annotated = 0
    if owner_id:
        meta = await get_collection("dataset_metadata").find_one(
            {"dataset_name": dataset_name, "user_id": owner_id, "is_active": True}
        )
        if meta:
            dataset_id = meta["_id"]
            q: dict = {
                "user_id": owner_id,
                "dataset_id": dataset_id,
                "image_id": {"$gte": batch.get("start_index", 1), "$lte": batch.get("end_index", 1)},
            }
            annotated_ids = await get_collection("annotations").distinct("image_id", q)
            annotated = len(set(annotated_ids))

    total = batch.get("image_count", 0)
    pending = max(0, total - annotated)
    pct = round(100.0 * annotated / total, 1) if total > 0 else 0.0

    response = _format_batch_response(batch)
    return TaskBatchWithProgress(
        **response.model_dump(),
        images_annotated=annotated,
        images_pending=pending,
        progress_percentage=pct,
    )
