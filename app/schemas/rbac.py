"""Pydantic schemas for RBAC (Role-Based Access Control) and task distribution."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ─── enums ──────────────────────────────────────────────────────────────────

class UserRole(str, Enum):
    """System-wide user roles."""
    ADMIN = "admin"
    ANNOTATOR = "annotator"
    CHECKER = "checker"  # Reviewer


class TaskStatus(str, Enum):
    """Task/Batch status states."""
    PENDING = "pending"
    ASSIGNED = "assigned"
    ANNOTATED = "annotated"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class AnnotationStatus(str, Enum):
    """Individual annotation status in workflow."""
    PENDING = "pending"
    ANNOTATED = "annotated"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"


# ─── user schemas ───────────────────────────────────────────────────────────

class UserBase(BaseModel):
    """Base user fields."""
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=120)
    role: UserRole = UserRole.ANNOTATOR


class UserCreateRequest(UserBase):
    """Create user request (admin only)."""
    password: str = Field(..., min_length=8, max_length=128)
    department: Optional[str] = Field(None, max_length=100)


class UserUpdateRequest(BaseModel):
    """Update user request (admin only)."""
    full_name: Optional[str] = Field(None, min_length=1, max_length=120)
    role: Optional[UserRole] = None
    department: Optional[str] = None
    is_active: Optional[bool] = None


class UserPublicWithRole(BaseModel):
    """User response with role information."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    email: EmailStr
    full_name: str
    role: UserRole
    department: Optional[str] = None
    is_active: bool = True
    created_at: datetime
    last_login_at: Optional[datetime] = None


class UserListResponse(BaseModel):
    """List of users with pagination."""
    users: List[UserPublicWithRole]
    total: int
    page: int
    page_size: int
    total_pages: int


# ─── task batch schemas ─────────────────────────────────────────────────────

class TaskBatchCreate(BaseModel):
    """Create task batch request."""
    project_id: str
    batch_number: int
    start_index: int
    end_index: int
    image_count: int
    deadline: Optional[datetime] = None


class TaskBatchUpdate(BaseModel):
    """Update task batch request."""
    status: Optional[TaskStatus] = None
    deadline: Optional[datetime] = None


class TaskBatchAssign(BaseModel):
    """Assign batch to annotator."""
    annotator_id: str
    due_date: Optional[datetime] = None


class TaskBatchResponse(BaseModel):
    """Task batch response."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    project_id: str
    batch_number: int
    start_index: int
    end_index: int
    image_count: int
    status: TaskStatus
    assigned_to: Optional[str] = None
    assigned_date: Optional[datetime] = None
    deadline: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class TaskBatchWithProgress(TaskBatchResponse):
    """Task batch with progress metrics."""
    images_annotated: int = 0
    images_pending: int = 0
    progress_percentage: float = 0.0


# ─── task assignment schemas ────────────────────────────────────────────────

class TaskAssignmentCreate(BaseModel):
    """Create task assignment."""
    batch_id: str
    annotator_id: str
    due_date: Optional[datetime] = None


class TaskAssignmentResponse(BaseModel):
    """Task assignment response."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    batch_id: str
    annotator_id: str
    status: TaskStatus
    assigned_date: datetime
    due_date: Optional[datetime] = None
    completion_date: Optional[datetime] = None
    images_annotated: int = 0
    images_pending: int = 0
    progress_percentage: float = 0.0


# ─── annotation schemas ──────────────────────────────────────────────────────

class AnnotationStatusUpdate(BaseModel):
    """Update annotation status."""
    status: AnnotationStatus


class AnnotationReviewResponse(BaseModel):
    """Individual annotation for review."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    image_id: str
    annotation_data: dict
    annotator_id: str
    status: AnnotationStatus
    created_at: datetime
    updated_at: datetime


class AnnotationApproveRequest(BaseModel):
    """Approve annotation request."""
    notes: Optional[str] = None


class AnnotationRejectRequest(BaseModel):
    """Reject annotation request."""
    reason: str = Field(..., min_length=1, max_length=500)
    notes: Optional[str] = None


class AnnotationReviewAudit(BaseModel):
    """Audit trail for annotation review."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    annotation_id: str
    reviewer_id: str
    action: str  # approve, reject
    reason: Optional[str] = None
    notes: Optional[str] = None
    previous_status: AnnotationStatus
    new_status: AnnotationStatus
    created_at: datetime


# ─── progress tracking schemas ──────────────────────────────────────────────

class BatchProgressStats(BaseModel):
    """Batch-level progress statistics."""
    batch_id: str
    total_images: int
    annotated: int
    under_review: int
    approved: int
    rejected: int
    pending: int
    progress_percentage: float


class ProjectProgressStats(BaseModel):
    """Project-level progress statistics."""
    project_id: str
    total_images: int
    total_batches: int
    active_batches: int
    annotated: int
    under_review: int
    approved: int
    rejected: int
    overall_progress_percentage: float
    by_batch: List[BatchProgressStats] = []


class UserProgressStats(BaseModel):
    """User progress statistics (per annotator/checker)."""
    user_id: str
    full_name: str
    role: UserRole
    assigned_batches: int
    completed_batches: int
    images_annotated: int
    images_reviewed: int
    avg_annotation_per_batch: float
    performance_score: float  # 0-100


# ─── permission schemas ─────────────────────────────────────────────────────

class PermissionSet(BaseModel):
    """Permission set for user on project."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    user_id: str
    project_id: str
    role: UserRole
    can_annotate: bool = False
    can_review: bool = False
    can_export: bool = False
    can_manage_users: bool = False
    can_create_batches: bool = False
    created_at: datetime
    updated_at: datetime


# ─── audit log schemas ──────────────────────────────────────────────────────

class AuditLogEntry(BaseModel):
    """Audit trail entry."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    user_id: str
    action: str  # create_project, assign_batch, approve_annotation, etc.
    resource_type: str  # project, batch, annotation, user
    resource_id: Optional[str] = None
    changes: Optional[dict] = None
    ip_address: Optional[str] = None
    created_at: datetime


class AuditLogFilter(BaseModel):
    """Filter for audit log queries."""
    user_id: Optional[str] = None
    action: Optional[str] = None
    resource_type: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=500)


# ─── batch workflow schemas ────────────────────────────────────────────────

class BatchSubmitRequest(BaseModel):
    """Annotator submits a completed batch for checker review."""
    notes: Optional[str] = None


class BatchReviewRequest(BaseModel):
    """Checker/admin approves or rejects a batch."""
    notes: Optional[str] = None
    reason: Optional[str] = None   # required for reject


# ─── export schemas (enhanced) ───────────────────────────────────────────────

class ExportRequest(BaseModel):
    """Request to export dataset (only approved annotations)."""
    project_id: str
    format: str = "coco"  # coco, yolo, etc.
    include_pending: bool = False  # If False, only approved
    batch_ids: Optional[List[str]] = None  # If specified, export only these batches


class ExportResponse(BaseModel):
    """Export response with status."""
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    project_id: str
    status: str  # pending, processing, completed, failed
    format: str
    file_url: Optional[str] = None
    error: Optional[str] = None
    created_by: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    approved_count: int = 0
    total_count: int = 0
