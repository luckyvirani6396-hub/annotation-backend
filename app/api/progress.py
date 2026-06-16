"""Progress tracking APIs for real-time status monitoring."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from loguru import logger
from bson import ObjectId
from datetime import datetime, timedelta

from app.auth.rbac_dependencies import require_admin
from app.auth.dependencies import get_current_active_user
from app.config.database import db_manager
from app.repositories import TaskBatchRepository, UserRepository
from app.schemas.rbac import (
    ProjectProgressStats,
    BatchProgressStats,
    UserProgressStats,
    UserRole,
)

router = APIRouter(prefix="/api/progress", tags=["Progress"])


# ─── project progress ───────────────────────────────────────────────────────


@router.get("/projects/{project_id}", response_model=dict)
async def get_project_progress(
    project_id: str,
    current_user: dict = Depends(require_admin),
):
    """
    Get progress statistics for entire project (admin only).
    
    Returns:
    - Total images and batches
    - Counts by status (pending, annotated, approved, rejected)
    - Overall completion percentage
    - Per-batch breakdown
    """
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    annotation_collection = db["annotations"]

    try:
        # Get all batches for project
        batches, total_batches = await batch_repo.get_batches_by_project(
            project_id=project_id,
            page=1,
            page_size=10000,  # Get all
        )

        if not batches:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found or has no batches",
            )

        # Calculate project-level stats
        total_images = sum(b["image_count"] for b in batches)

        # Get annotation stats
        annotations = await annotation_collection.find(
            {"project_id": project_id}
        ).to_list(None)

        stats = {
            "total_images": total_images,
            "total_batches": total_batches,
            "annotated": 0,
            "under_review": 0,
            "approved": 0,
            "rejected": 0,
            "pending": total_images,
            "by_batch": [],
        }

        for annotation in annotations:
            status_val = annotation.get("status", "pending")
            if status_val == "annotated":
                stats["annotated"] += 1
                stats["pending"] -= 1
            elif status_val == "under_review":
                stats["under_review"] += 1
                stats["pending"] -= 1
            elif status_val == "approved":
                stats["approved"] += 1
                stats["pending"] -= 1
            elif status_val == "rejected":
                stats["rejected"] += 1
                stats["pending"] -= 1

        # Calculate per-batch stats
        for batch in batches:
            batch_annotations = await annotation_collection.find(
                {"batch_id": str(batch["_id"])}
            ).to_list(None)

            batch_stats = _calculate_batch_progress(batch, batch_annotations)
            stats["by_batch"].append(batch_stats)

        stats["overall_progress_percentage"] = (
            ((stats["annotated"] + stats["under_review"] + stats["approved"] + stats["rejected"]) / stats["total_images"] * 100)
            if stats["total_images"] > 0
            else 0
        )
        stats["approval_rate"] = (
            (stats["approved"] / (stats["approved"] + stats["rejected"]) * 100)
            if (stats["approved"] + stats["rejected"]) > 0
            else 0
        )

        return {
            "success": True,
            "data": stats,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting project progress: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get project progress",
        )


# ─── batch progress ─────────────────────────────────────────────────────────


@router.get("/batches/{batch_id}", response_model=dict)
async def get_batch_progress(
    batch_id: str,
    current_user: dict = Depends(require_admin),
):
    """Get progress for specific batch."""
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    annotation_collection = db["annotations"]

    try:
        batch = await batch_repo.get_batch_by_id(batch_id)
        if not batch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Batch not found",
            )

        # Get annotations for batch
        annotations = await annotation_collection.find(
            {"batch_id": batch_id}
        ).to_list(None)

        stats = _calculate_batch_progress(batch, annotations)

        return {
            "success": True,
            "data": stats,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting batch progress: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get batch progress",
        )


# ─── user progress ──────────────────────────────────────────────────────────


@router.get("/user/me", response_model=dict)
async def get_my_progress(
    current_user: dict = Depends(get_current_active_user),
):
    """Get current user's progress statistics."""
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    annotation_collection = db["annotations"]
    user_repo = UserRepository(db)

    try:
        user_id = str(current_user["_id"])
        user = await user_repo.get_user_by_id(user_id)

        # Get user's assigned batches
        batches = await batch_repo.get_batches_by_annotator(user_id)

        if not batches:
            return {
                "success": True,
                "data": {
                    "user_id": user_id,
                    "full_name": user.get("full_name"),
                    "role": user.get("role", "annotator"),
                    "assigned_batches": 0,
                    "completed_batches": 0,
                    "images_annotated": 0,
                    "images_reviewed": 0,
                    "performance_score": 0.0,
                },
            }

        # Count completed batches
        completed = sum(1 for b in batches if b.get("status") in ["submitted", "under_review", "approved"])

        # Get active datasets to filter out deleted/inactive ones
        active_datasets = await db["dataset_metadata"].find({"is_active": True}).to_list(None)
        active_dataset_ids = [str(d["_id"]) for d in active_datasets]

        # Get all annotations by user (either via annotator_id or fallback to user_id if annotator_id is missing)
        ann_query = {
            "$or": [
                {"annotator_id": user_id},
                {"annotator_id": {"$exists": False}, "user_id": user_id}
            ]
        }
        if active_dataset_ids:
            ann_query["dataset_id"] = {"$in": active_dataset_ids}
        else:
            ann_query["dataset_id"] = "__none__"

        annotations = await annotation_collection.find(ann_query).to_list(None)

        # Get reviews by user (if checker)
        reviews = await db["annotation_reviews"].find(
            {"reviewer_id": user_id}
        ).to_list(None)

        # Calculate performance score (0-100)
        total_annotations = len(annotations)
        approved_count = sum(1 for a in annotations if a.get("status") == "approved")
        rejected_count = sum(1 for a in annotations if a.get("status") == "rejected")

        performance_score = 0.0
        if total_annotations > 0:
            performance_score = (approved_count / total_annotations) * 100

        stats = {
            "user_id": user_id,
            "full_name": user.get("full_name"),
            "role": user.get("role", "annotator"),
            "assigned_batches": len(batches),
            "completed_batches": completed,
            "images_annotated": total_annotations,
            "images_reviewed": len(reviews),
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "performance_score": round(performance_score, 2),
        }

        return {
            "success": True,
            "data": stats,
        }

    except Exception as e:
        logger.error(f"Error getting user progress: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user progress",
        )


@router.get("/users/{user_id}", response_model=dict)
async def get_user_progress(
    user_id: str,
    current_user: dict = Depends(require_admin),
):
    """Get progress for specific user (admin only)."""
    db = db_manager.get_db()
    batch_repo = TaskBatchRepository(db)
    annotation_collection = db["annotations"]
    user_repo = UserRepository(db)

    try:
        user = await user_repo.get_user_by_id(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        # Get user's assigned batches
        batches = await batch_repo.get_batches_by_annotator(user_id)

        if not batches:
            return {
                "success": True,
                "data": {
                    "user_id": user_id,
                    "full_name": user.get("full_name"),
                    "role": user.get("role", "annotator"),
                    "assigned_batches": 0,
                    "completed_batches": 0,
                    "images_annotated": 0,
                    "images_reviewed": 0,
                    "performance_score": 0.0,
                },
            }

        # Count completed batches
        completed = sum(1 for b in batches if b.get("status") in ["submitted", "under_review", "approved"])

        # Get active datasets to filter out deleted/inactive ones
        active_datasets = await db["dataset_metadata"].find({"is_active": True}).to_list(None)
        active_dataset_ids = [str(d["_id"]) for d in active_datasets]

        # Get all annotations by user (either via annotator_id or fallback to user_id if annotator_id is missing)
        ann_query = {
            "$or": [
                {"annotator_id": user_id},
                {"annotator_id": {"$exists": False}, "user_id": user_id}
            ]
        }
        if active_dataset_ids:
            ann_query["dataset_id"] = {"$in": active_dataset_ids}
        else:
            ann_query["dataset_id"] = "__none__"

        annotations = await annotation_collection.find(ann_query).to_list(None)

        # Get reviews by user
        reviews = await db["annotation_reviews"].find(
            {"reviewer_id": user_id}
        ).to_list(None)

        # Calculate performance score
        total_annotations = len(annotations)
        approved_count = sum(1 for a in annotations if a.get("status") == "approved")

        performance_score = 0.0
        if total_annotations > 0:
            performance_score = (approved_count / total_annotations) * 100

        stats = {
            "user_id": user_id,
            "full_name": user.get("full_name"),
            "role": user.get("role", "annotator"),
            "assigned_batches": len(batches),
            "completed_batches": completed,
            "images_annotated": total_annotations,
            "images_reviewed": len(reviews),
            "performance_score": round(performance_score, 2),
        }

        return {
            "success": True,
            "data": stats,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user progress: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user progress",
        )


# ─── team/admin dashboard progress ───────────────────────────────────────────


@router.get("/dashboard/team", response_model=dict)
async def get_team_progress(
    current_user: dict = Depends(require_admin),
):
    """Get team-wide progress dashboard (admin only)."""
    db = db_manager.get_db()
    user_repo = UserRepository(db)
    batch_repo = TaskBatchRepository(db)
    annotation_collection = db["annotations"]

    try:
        # Get all active users
        users, total_users = await user_repo.list_users(page=1, page_size=1000, is_active=True)

        # Get active datasets to filter out deleted/inactive ones
        active_datasets = await db["dataset_metadata"].find({"is_active": True}).to_list(None)
        active_dataset_ids = [str(d["_id"]) for d in active_datasets]

        # Build user stats
        user_stats = []
        for user in users:
            user_id = str(user["_id"])
            batches = await batch_repo.get_batches_by_annotator(user_id)
            # Get all annotations by user (either via annotator_id or fallback to user_id if annotator_id is missing)
            ann_query = {
                "$or": [
                    {"annotator_id": user_id},
                    {"annotator_id": {"$exists": False}, "user_id": user_id}
                ]
            }
            if active_dataset_ids:
                ann_query["dataset_id"] = {"$in": active_dataset_ids}
            else:
                ann_query["dataset_id"] = "__none__"

            annotations = await annotation_collection.find(ann_query).to_list(None)

            approved = sum(1 for a in annotations if a.get("status") == "approved")
            perf_score = (approved / len(annotations) * 100) if annotations else 0

            user_stats.append({
                "user_id": user_id,
                "full_name": user.get("full_name"),
                "role": user.get("role"),
                "assigned_batches": len(batches),
                "annotations": len(annotations),
                "approved": approved,
                "performance_score": round(perf_score, 2),
            })

        # Get overall stats
        all_ann_query = {}
        if active_dataset_ids:
            all_ann_query["dataset_id"] = {"$in": active_dataset_ids}
        else:
            all_ann_query["dataset_id"] = "__none__"

        all_annotations = await annotation_collection.find(all_ann_query).to_list(None)
        stats = {
            "total_active_users": total_users,
            "total_annotations": len(all_annotations),
            "approved": sum(1 for a in all_annotations if a.get("status") == "approved"),
            "rejected": sum(1 for a in all_annotations if a.get("status") == "rejected"),
            "pending": sum(1 for a in all_annotations if a.get("status", "pending") == "pending"),
            "user_stats": sorted(user_stats, key=lambda x: x["performance_score"], reverse=True),
        }

        return {
            "success": True,
            "data": stats,
        }

    except Exception as e:
        logger.error(f"Error getting team progress: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get team progress",
        )


# ─── helper functions ───────────────────────────────────────────────────────


def _calculate_batch_progress(batch: dict, annotations: list) -> dict:
    """Calculate progress stats for a batch."""
    total = batch.get("image_count", 0)

    stats = {
        "pending": 0,
        "annotated": 0,
        "under_review": 0,
        "approved": 0,
        "rejected": 0,
    }

    for annotation in annotations:
        status_val = annotation.get("status", "pending")
        if status_val in stats:
            stats[status_val] += 1

    # Count pending as images without annotation
    stats["pending"] = total - len(annotations)

    completion_pct = (
        ((total - stats["pending"]) / total * 100) if total > 0 else 0
    )

    return {
        "batch_id": str(batch["_id"]),
        "batch_number": batch["batch_number"],
        "total_images": total,
        "annotated": stats["annotated"],
        "under_review": stats["under_review"],
        "approved": stats["approved"],
        "rejected": stats["rejected"],
        "pending": stats["pending"],
        "progress_percentage": round(completion_pct, 2),
        "approval_rate": (
            (stats["approved"] / (stats["approved"] + stats["rejected"]) * 100)
            if (stats["approved"] + stats["rejected"]) > 0
            else 0
        ),
    }
