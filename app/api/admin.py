"""Admin APIs for RBAC management (user management, permissions, etc.)."""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from loguru import logger

from app.auth.rbac_dependencies import require_admin
from app.auth.security import hash_password
from app.config.database import db_manager
from app.repositories import UserRepository, AuditLogRepository
from app.schemas.rbac import (
    UserRole,
    UserCreateRequest,
    UserUpdateRequest,
    UserListResponse,
    UserPublicWithRole,
)

router = APIRouter(prefix="/api/admin", tags=["admin_create_users"])


# ─── user management ────────────────────────────────────────────────────────


@router.post("/users", response_model=UserPublicWithRole)
async def create_user(
    request: UserCreateRequest,
    current_user: dict = Depends(require_admin),
):
    """
    Create new user (admin only).
    
    Roles available:
    - admin: Full system access
    - annotator: Can annotate assigned images
    - checker: Can review annotations
    """
    db = db_manager.get_db()
    user_repo = UserRepository(db)
    audit_repo = AuditLogRepository(db)

    # Check if email already exists
    existing = await user_repo.get_user_by_email(request.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    try:
        # Create user
        user_id = await user_repo.create_user(
            email=request.email,
            password_hash=hash_password(request.password),
            full_name=request.full_name,
            role=request.role,
            department=request.department,
        )

        # Log audit trail
        await audit_repo.log_action(
            user_id=current_user["_id"],
            action="create_user",
            resource_type="user",
            resource_id=user_id,
            changes={
                "email": request.email,
                "full_name": request.full_name,
                "role": request.role.value,
            },
        )

        user = await user_repo.get_user_by_id(user_id)
        return _format_user_response(user)

    except Exception as e:
        logger.error(f"Error creating user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user",
        )


@router.get("/users", response_model=UserListResponse)
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    role: str = Query(None),
    is_active: bool = Query(None),
    current_user: dict = Depends(require_admin),
):
    """
    List all users (admin only).
    
    Query parameters:
    - page: Page number (1-indexed)
    - page_size: Items per page (1-500)
    - role: Filter by role (admin, annotator, checker)
    - is_active: Filter by active status
    """
    db = db_manager.get_db()
    user_repo = UserRepository(db)

    try:
        # Parse role filter
        role_filter = None
        if role:
            try:
                role_filter = UserRole(role)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid role: {role}",
                )

        users, total = await user_repo.list_users(
            page=page,
            page_size=page_size,
            role=role_filter,
            is_active=is_active,
        )

        total_pages = (total + page_size - 1) // page_size

        return UserListResponse(
            users=[_format_user_response(u) for u in users],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    except Exception as e:
        logger.error(f"Error listing users: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list users",
        )


@router.get("/users/{user_id}", response_model=UserPublicWithRole)
async def get_user(
    user_id: str,
    current_user: dict = Depends(require_admin),
):
    """Get user details (admin only)."""
    db = db_manager.get_db()
    user_repo = UserRepository(db)

    user = await user_repo.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return _format_user_response(user)


@router.put("/users/{user_id}", response_model=UserPublicWithRole)
async def update_user(
    user_id: str,
    request: UserUpdateRequest,
    current_user: dict = Depends(require_admin),
):
    """Update user (admin only)."""
    db = db_manager.get_db()
    user_repo = UserRepository(db)
    audit_repo = AuditLogRepository(db)

    # Get existing user
    user = await user_repo.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    try:
        # Prepare update data
        update_fields = {}
        changes = {}

        if request.full_name is not None:
            update_fields["full_name"] = request.full_name
            changes["full_name"] = request.full_name

        if request.role is not None:
            update_fields["role"] = request.role.value
            changes["role"] = request.role.value

        if request.department is not None:
            update_fields["department"] = request.department
            changes["department"] = request.department

        if request.is_active is not None:
            update_fields["is_active"] = request.is_active
            changes["is_active"] = request.is_active

        if request.password is not None:
            update_fields["password_hash"] = hash_password(request.password)
            changes["password"] = "[REDACTED]"

        # Update user
        if update_fields:
            await user_repo.update_user(user_id, **update_fields)
            
            # Log audit trail
            await audit_repo.log_action(
                user_id=current_user["_id"],
                action="update_user",
                resource_type="user",
                resource_id=user_id,
                changes=changes,
            )

        # Get updated user
        updated_user = await user_repo.get_user_by_id(user_id)
        return _format_user_response(updated_user)

    except Exception as e:
        logger.error(f"Error updating user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user",
        )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    current_user: dict = Depends(require_admin),
):
    """Permanently delete a user (admin only)."""
    db = db_manager.get_db()
    user_repo = UserRepository(db)
    audit_repo = AuditLogRepository(db)

    # Prevent self-deletion
    if str(current_user["_id"]) == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    # Get user
    user = await user_repo.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    try:
        await user_repo.delete_user(user_id)

        # Log audit trail
        await audit_repo.log_action(
            user_id=current_user["_id"],
            action="delete_user",
            resource_type="user",
            resource_id=user_id,
            changes={"email": user.get("email")},
        )

    except Exception as e:
        logger.error(f"Error deleting user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete user",
        )


# ─── helper functions ───────────────────────────────────────────────────────


def _format_user_response(user: dict) -> UserPublicWithRole:
    """Format user document to response model."""
    return UserPublicWithRole(
        _id=str(user["_id"]),
        email=user["email"],
        full_name=user["full_name"],
        role=UserRole(user.get("role", "annotator")),
        department=user.get("department"),
        is_active=user.get("is_active", True),
        created_at=user.get("created_at"),
        last_login_at=user.get("last_login_at"),
    )
