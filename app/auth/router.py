"""HTTP routes for registration, login, refresh, and the current-user probe."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError
from loguru import logger
from pymongo.errors import DuplicateKeyError

from app.auth.dependencies import get_current_active_user
from app.auth.repository import (
    create_user,
    delete_user,
    get_user_by_email,
    get_user_by_id,
    touch_last_login,
    update_user_password,
    update_user_profile,
)
from app.auth.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    DeleteAccountRequest,
    RefreshTokenRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserLoginRequest,
    UserPublic,
    UserRegisterRequest,
)
from app.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)

router = APIRouter()


# ─── helpers ─────────────────────────────────────────────────────────────────


def _issue_tokens(user_id: str, role: str = "annotator") -> TokenResponse:
    """Issue access and refresh tokens with role information."""
    access, expires_in = create_access_token(
        user_id,
        extra={"role": role}
    )
    refresh = create_refresh_token(user_id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
    )


def _public_user(doc: dict) -> UserPublic:
    return UserPublic.model_validate(doc)


# ─── routes ──────────────────────────────────────────────────────────────────


# @router.post(
#     "/register",
#     response_model=AuthResponse,
#     status_code=status.HTTP_201_CREATED,
#     summary="Create a new account",
# )
# async def register(payload: UserRegisterRequest) -> AuthResponse:
#     try:
#         user = await create_user(
#             email=payload.email,
#             full_name=payload.full_name,
#             password_hash=hash_password(payload.password),
#         )
#     except DuplicateKeyError:
#         raise HTTPException(
#             status_code=status.HTTP_409_CONFLICT,
#             detail="An account with this email already exists",
#         )

#     logger.info(f"Registered new user {user['email']}")
#     tokens = _issue_tokens(
#         user["_id"],
#         role=user.get("role", "annotator")
#     )
#     await touch_last_login(user["_id"])
#     return AuthResponse(user=_public_user(user), tokens=tokens)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Exchange credentials for a token pair",
)
async def login(payload: UserLoginRequest) -> AuthResponse:
    user = await get_user_by_email(payload.email)
    if user is None or not verify_password(payload.password, user["password_hash"]):
        # Same error for "no such user" and "wrong password" to avoid
        # enumeration.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    tokens = _issue_tokens(
        user["_id"],
        role=user.get("role", "annotator")
    )
    await touch_last_login(user["_id"])
    user["last_login_at"] = user.get("last_login_at")  # refreshed value irrelevant for response
    return AuthResponse(user=_public_user(user), tokens=tokens)


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="OAuth2 compatible token endpoint (Swagger UI authorization)",
)
async def login_oauth2(form_data: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    """
    OAuth2 compatible login endpoint for Swagger UI.
    Accepts form-encoded username (email) and password.
    Returns JWT tokens for API authentication.
    
    Use email as the username in Swagger UI authorization dialog.
    """
    user = await get_user_by_email(form_data.username)  # username is the email
    if user is None or not verify_password(form_data.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    tokens = _issue_tokens(
        user["_id"],
        role=user.get("role", "annotator")
    )
    await touch_last_login(user["_id"])
    return tokens


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Trade a refresh token for a fresh access token",
)
async def refresh(payload: RefreshTokenRequest) -> TokenResponse:
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    user = await get_user_by_id(claims["sub"])
    if user is None or not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account no longer valid",
        )
    return _issue_tokens(
        user["_id"],
        role=user.get("role", "annotator")
    )


@router.get(
    "/me",
    response_model=UserPublic,
    summary="Return the authenticated user",
)
async def me(user: dict = Depends(get_current_active_user)) -> UserPublic:
    return _public_user(user)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Client-side logout (stateless)",
)
async def logout() -> Response:
    """No-op: with stateless JWT the client just discards its tokens.

    Kept as an endpoint so the frontend has a stable place to call.
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ─── account self-service ───────────────────────────────────────────────────


@router.patch(
    "/me",
    response_model=UserPublic,
    summary="Update editable profile fields (full name, …)",
)
async def update_me(
    payload: UpdateProfileRequest,
    user: dict = Depends(get_current_active_user),
) -> UserPublic:
    if payload.full_name is None:
        # Nothing to change — return current user untouched.
        return _public_user(user)

    fresh = await update_user_profile(user["_id"], full_name=payload.full_name)
    if fresh is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User no longer exists",
        )
    logger.info("Updated profile for user {}", fresh["email"])
    return _public_user(fresh)


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Change the current user's password",
)
async def change_password(
    payload: ChangePasswordRequest,
    user: dict = Depends(get_current_active_user),
) -> Response:
    # Confirm the caller knows the current password.
    if not verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    # Reject trivial "change" to the same password.
    if verify_password(payload.new_password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current one",
        )

    ok = await update_user_password(
        user["_id"], password_hash=hash_password(payload.new_password)
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update password",
        )
    logger.info("Password changed for user {}", user["email"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete the current user's account (requires password confirmation)",
)
async def delete_me(
    payload: DeleteAccountRequest,
    user: dict = Depends(get_current_active_user),
) -> Response:
    if not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password is incorrect",
        )
    ok = await delete_user(user["_id"])
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete account",
        )
    logger.warning("Deleted account for user {}", user["email"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
