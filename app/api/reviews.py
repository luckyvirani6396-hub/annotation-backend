"""Annotation review workflow APIs (approve/reject annotations)."""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from loguru import logger
from bson import ObjectId

from app.auth.rbac_dependencies import require_checker_or_admin, require_annotator_or_admin
from app.config.database import db_manager
from app.repositories import (
    AnnotationReviewRepository,
    TaskBatchRepository,
    AuditLogRepository,
)
from app.schemas.rbac import (
    AnnotationStatus,
    AnnotationApproveRequest,
    AnnotationRejectRequest,
    AnnotationReviewAudit,
)

router = APIRouter(prefix="/api/annotations", tags=["Reviews"])


# ─── review workflow ────────────────────────────────────────────────────────


@router.post("/{annotation_id}/approve", response_model=dict)
async def approve_annotation(
    annotation_id: str,
    request: AnnotationApproveRequest,
    current_user: dict = Depends(require_checker_or_admin),
):
    """
    Approve an annotation (checker or admin only).
    
    Moves annotation from UNDER_REVIEW to APPROVED status.
    Only approved annotations can be exported.
    """
    db = db_manager.get_db()
    annotation_collection = db["annotations"]
    review_repo = AnnotationReviewRepository(db)
    audit_repo = AuditLogRepository(db)

    try:
        # Get annotation
        if not ObjectId.is_valid(annotation_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid annotation ID",
            )

        annotation = await annotation_collection.find_one({"_id": ObjectId(annotation_id)})
        if not annotation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Annotation not found",
            )

        # Verify it's under review
        current_status = AnnotationStatus(annotation.get("status", "pending"))
        if current_status != AnnotationStatus.UNDER_REVIEW:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot approve annotation in {current_status.value} status",
            )

        # Update annotation
        await annotation_collection.update_one(
            {"_id": ObjectId(annotation_id)},
            {
                "$set": {
                    "status": AnnotationStatus.APPROVED.value,
                    "reviewed_by": str(current_user["_id"]),
                    "review_date": datetime.utcnow(),
                    "review_notes": request.notes or "",
                }
            }
        )

        # Create audit trail
        await review_repo.create_review(
            annotation_id=annotation_id,
            reviewer_id=str(current_user["_id"]),
            action="approve",
            previous_status=current_status,
            new_status=AnnotationStatus.APPROVED,
            notes=request.notes,
        )

        # Log audit trail
        await audit_repo.log_action(
            user_id=str(current_user["_id"]),
            action="approve_annotation",
            resource_type="annotation",
            resource_id=annotation_id,
            changes={
                "status": AnnotationStatus.APPROVED.value,
            },
        )

        return {
            "success": True,
            "message": "Annotation approved",
            "data": {
                "annotation_id": annotation_id,
                "status": AnnotationStatus.APPROVED.value,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error approving annotation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to approve annotation",
        )


@router.post("/{annotation_id}/reject", response_model=dict)
async def reject_annotation(
    annotation_id: str,
    request: AnnotationRejectRequest,
    current_user: dict = Depends(require_checker_or_admin),
):
    """
    Reject an annotation (checker or admin only).
    
    Moves annotation from UNDER_REVIEW back to REJECTED status.
    Rejected annotations can be fixed and resubmitted.
    """
    db = db_manager.get_db()
    annotation_collection = db["annotations"]
    review_repo = AnnotationReviewRepository(db)
    audit_repo = AuditLogRepository(db)

    try:
        # Get annotation
        if not ObjectId.is_valid(annotation_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid annotation ID",
            )

        annotation = await annotation_collection.find_one({"_id": ObjectId(annotation_id)})
        if not annotation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Annotation not found",
            )

        # Verify it's under review
        current_status = AnnotationStatus(annotation.get("status", "pending"))
        if current_status != AnnotationStatus.UNDER_REVIEW:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot reject annotation in {current_status.value} status",
            )

        # Update annotation
        await annotation_collection.update_one(
            {"_id": ObjectId(annotation_id)},
            {
                "$set": {
                    "status": AnnotationStatus.REJECTED.value,
                    "reviewed_by": str(current_user["_id"]),
                    "review_date": datetime.utcnow(),
                    "rejection_reason": request.reason,
                    "review_notes": request.notes or "",
                }
            }
        )

        # Create audit trail
        await review_repo.create_review(
            annotation_id=annotation_id,
            reviewer_id=str(current_user["_id"]),
            action="reject",
            previous_status=current_status,
            new_status=AnnotationStatus.REJECTED,
            reason=request.reason,
            notes=request.notes,
        )

        # Log audit trail
        await audit_repo.log_action(
            user_id=str(current_user["_id"]),
            action="reject_annotation",
            resource_type="annotation",
            resource_id=annotation_id,
            changes={
                "status": AnnotationStatus.REJECTED.value,
                "rejection_reason": request.reason,
            },
        )

        return {
            "success": True,
            "message": "Annotation rejected",
            "data": {
                "annotation_id": annotation_id,
                "status": AnnotationStatus.REJECTED.value,
                "reason": request.reason,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rejecting annotation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reject annotation",
        )


# ─── review listing ─────────────────────────────────────────────────────────


@router.get("/pending-review/list", response_model=dict)
async def list_pending_reviews(
    batch_id: str = Query(None),
    project_id: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(require_checker_or_admin),
):
    """
    List annotations pending review (checker or admin only).
    
    Query parameters:
    - batch_id: Filter by batch (optional)
    - project_id: Filter by project (optional)
    - page: Page number
    - page_size: Items per page
    """
    db = db_manager.get_db()
    annotation_collection = db["annotations"]

    try:
        # Build query
        query = {"status": AnnotationStatus.ANNOTATED.value}

        if batch_id:
            query["batch_id"] = batch_id
        if project_id:
            query["project_id"] = project_id

        # Count total
        total = await annotation_collection.count_documents(query)

        # Fetch paginated results
        skip = (page - 1) * page_size
        annotations = await annotation_collection.find(query).skip(skip).limit(page_size).to_list(None)

        total_pages = (total + page_size - 1) // page_size

        return {
            "success": True,
            "data": {
                "annotations": [_format_annotation(a) for a in annotations],
                "pagination": {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                },
            },
        }

    except Exception as e:
        logger.error(f"Error listing pending reviews: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list pending reviews",
        )


@router.get("/batch/{batch_id}/review", response_model=dict)
async def get_batch_for_review(
    batch_id: str,
    status: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    current_user: dict = Depends(require_checker_or_admin),
):
    """
    Get all annotations for a batch to review (checker or admin only).
    
    Query parameters:
    - status: Filter by status (pending, annotated, under_review, approved, rejected)
    - page: Page number
    - page_size: Items per page
    """
    db = db_manager.get_db()
    annotation_collection = db["annotations"]
    batch_repo = TaskBatchRepository(db)

    try:
        # Verify batch exists
        batch = await batch_repo.get_batch_by_id(batch_id)
        if not batch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Batch not found",
            )

        # Build query
        query = {"batch_id": batch_id}
        if status:
            try:
                query["status"] = AnnotationStatus(status).value
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status: {status}",
                )

        # Count total
        total = await annotation_collection.count_documents(query)

        # Fetch paginated results
        skip = (page - 1) * page_size
        annotations = await annotation_collection.find(query).skip(skip).limit(page_size).to_list(None)

        total_pages = (total + page_size - 1) // page_size

        # Calculate stats
        all_annotations = await annotation_collection.find({"batch_id": batch_id}).to_list(None)
        stats = _calculate_batch_stats(all_annotations)

        return {
            "success": True,
            "data": {
                "batch_id": batch_id,
                "batch_number": batch["batch_number"],
                "annotations": [_format_annotation(a) for a in annotations],
                "stats": stats,
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
        logger.error(f"Error getting batch for review: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get batch for review",
        )


@router.get("/{annotation_id}/history", response_model=dict)
async def get_annotation_history(
    annotation_id: str,
    current_user: dict = Depends(require_checker_or_admin),
):
    """Get full review history for an annotation."""
    db = db_manager.get_db()
    review_repo = AnnotationReviewRepository(db)

    try:
        if not ObjectId.is_valid(annotation_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid annotation ID",
            )

        history = await review_repo.get_review_history(annotation_id)

        return {
            "success": True,
            "data": {
                "annotation_id": annotation_id,
                "history": [_format_review(r) for r in history],
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting annotation history: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get annotation history",
        )


# ─── helper functions ───────────────────────────────────────────────────────


def _format_annotation(annotation: dict) -> dict:
    """Format annotation for response."""
    return {
        "id": str(annotation["_id"]),
        "image_id": annotation.get("image_id"),
        "batch_id": annotation.get("batch_id"),
        "annotation_data": annotation.get("annotation_data", {}),
        "annotator_id": annotation.get("annotator_id"),
        "status": annotation.get("status", "pending"),
        "reviewed_by": annotation.get("reviewed_by"),
        "created_at": annotation.get("created_at"),
        "updated_at": annotation.get("updated_at"),
        "review_date": annotation.get("review_date"),
    }


def _format_review(review: dict) -> AnnotationReviewAudit:
    """Format review audit entry."""
    return AnnotationReviewAudit(
        _id=str(review["_id"]),
        annotation_id=review["annotation_id"],
        reviewer_id=review["reviewer_id"],
        action=review["action"],
        reason=review.get("reason"),
        notes=review.get("notes"),
        previous_status=AnnotationStatus(review["previous_status"]),
        new_status=AnnotationStatus(review["new_status"]),
        created_at=review["created_at"],
    )


def _calculate_batch_stats(annotations: list) -> dict:
    """Calculate statistics for a batch of annotations."""
    stats = {
        "total": len(annotations),
        "pending": 0,
        "annotated": 0,
        "under_review": 0,
        "approved": 0,
        "rejected": 0,
    }

    for annotation in annotations:
        status = annotation.get("status", "pending")
        if status in stats:
            stats[status] += 1

    stats["completion_percentage"] = (
        ((stats["annotated"] + stats["under_review"] + stats["approved"] + stats["rejected"]) / stats["total"] * 100)
        if stats["total"] > 0
        else 0
    )
    stats["approval_rate"] = (
        (stats["approved"] / (stats["approved"] + stats["rejected"]) * 100)
        if (stats["approved"] + stats["rejected"]) > 0
        else 0
    )

    return stats
