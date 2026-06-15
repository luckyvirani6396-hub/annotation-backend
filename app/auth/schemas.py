"""Pydantic schemas for the authentication API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ─── request models ──────────────────────────────────────────────────────────


class UserRegisterRequest(BaseModel):
    """Payload accepted by ``POST /api/auth/register``."""

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(..., min_length=1, max_length=120)


class UserLoginRequest(BaseModel):
    """JSON payload accepted by ``POST /api/auth/login``."""

    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., min_length=10)


class UpdateProfileRequest(BaseModel):
    """Payload accepted by ``PATCH /api/auth/me`` — partial profile edit."""

    full_name: Optional[str] = Field(None, min_length=1, max_length=120)


class ChangePasswordRequest(BaseModel):
    """Payload accepted by ``POST /api/auth/change-password``."""

    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class DeleteAccountRequest(BaseModel):
    """Payload accepted by ``DELETE /api/auth/me`` — requires password confirm."""

    password: str = Field(..., min_length=1, max_length=128)


# ─── response models ─────────────────────────────────────────────────────────


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until ``access_token`` expires


class UserPublic(BaseModel):
    """User record exposed to the client (no password hash)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., alias="_id")
    email: EmailStr
    full_name: str
    is_active: bool = True
    role: str = "annotator"
    department: Optional[str] = None
    created_at: datetime
    last_login_at: Optional[datetime] = None
    password_changed_at: Optional[datetime] = None


class AuthResponse(BaseModel):
    """Returned by ``/register`` and ``/login`` — bundles tokens + user."""

    user: UserPublic
    tokens: TokenResponse
