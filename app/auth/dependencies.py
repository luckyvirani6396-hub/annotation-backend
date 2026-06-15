"""FastAPI dependencies for authentication."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError

from app.auth.repository import get_user_by_id
from app.auth.security import decode_token

# ``tokenUrl`` points at our OAuth2 compatible token endpoint so Swagger's "Authorize"
# button works.  ``auto_error=True`` makes missing/empty headers a 401.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=True)


_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """Resolve ``Authorization: Bearer <token>`` into a user document."""
    try:
        payload = decode_token(token, expected_type="access")
    except JWTError:
        raise _CREDENTIALS_EXC

    user = await get_user_by_id(payload["sub"])
    if user is None:
        raise _CREDENTIALS_EXC
    return user


async def get_current_active_user(
    user: dict = Depends(get_current_user),
) -> dict:
    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )
    return user
