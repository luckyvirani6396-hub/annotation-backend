"""RBAC-aware authentication dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from jose import JWTError

from app.auth.dependencies import oauth2_scheme, get_current_user, get_current_active_user
from app.auth.security import decode_token
from app.schemas.rbac import UserRole
from app.repositories import UserRepository
from app.config.database import db_manager


async def get_current_user_with_role(
    user: dict = Depends(get_current_active_user),
) -> dict:
    """Get current user and ensure they have a role."""
    if "role" not in user:
        # Fallback for users without role (shouldn't happen with new system)
        user["role"] = UserRole.ANNOTATOR.value
    return user


async def require_role(required_role: UserRole):
    """Dependency factory to require specific role."""
    async def check_role(user: dict = Depends(get_current_user_with_role)) -> dict:
        user_role = UserRole(user.get("role", UserRole.ANNOTATOR.value))
        
        # Admin has access to everything
        if user_role == UserRole.ADMIN:
            return user
        
        # Check if user has required role
        if user_role != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This operation requires {required_role.value} role",
            )
        return user
    return check_role


async def require_admin(
    user: dict = Depends(get_current_user_with_role),
) -> dict:
    """Require admin role."""
    user_role = UserRole(user.get("role", UserRole.ANNOTATOR.value))
    if user_role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


async def require_checker_or_admin(
    user: dict = Depends(get_current_user_with_role),
) -> dict:
    """Require checker or admin role."""
    user_role = UserRole(user.get("role", UserRole.ANNOTATOR.value))
    if user_role not in [UserRole.CHECKER, UserRole.ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Checker or Admin access required",
        )
    return user


async def require_annotator_or_admin(
    user: dict = Depends(get_current_user_with_role),
) -> dict:
    """Require annotator or admin role."""
    user_role = UserRole(user.get("role", UserRole.ANNOTATOR.value))
    if user_role not in [UserRole.ANNOTATOR, UserRole.ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Annotator or Admin access required",
        )
    return user
