"""Password hashing and JWT helpers.

* Passwords are hashed with bcrypt via :mod:`passlib`.
* Tokens are signed with HS256 using ``settings.JWT_SECRET``.
* Two tokens are issued per session: a short-lived access token and a
  longer-lived refresh token (different ``token_type`` claim).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config.settings import settings

# bcrypt is the industry default; 12 rounds is a good speed/security balance
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ─── passwords ───────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Return a bcrypt hash of ``password``."""
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time compare ``plain`` against ``hashed``."""
    try:
        return _pwd_context.verify(plain, hashed)
    except ValueError:
        # Malformed hash in DB — treat as invalid rather than 500.
        return False


# ─── tokens ──────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(subject: str, *, extra: Optional[dict] = None) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``."""
    expires_in = settings.JWT_ACCESS_TTL_MINUTES * 60
    payload: dict[str, Any] = {
        "sub": subject,
        "type": "access",
        "iat": int(_now().timestamp()),
        "exp": int((_now() + timedelta(seconds=expires_in)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return _encode(payload), expires_in


def create_refresh_token(subject: str) -> str:
    payload = {
        "sub": subject,
        "type": "refresh",
        "iat": int(_now().timestamp()),
        "exp": int(
            (_now() + timedelta(days=settings.JWT_REFRESH_TTL_DAYS)).timestamp()
        ),
    }
    return _encode(payload)


def decode_token(token: str, *, expected_type: str = "access") -> dict[str, Any]:
    """Decode ``token`` and validate its ``type`` claim.

    Raises :class:`jose.JWTError` on any failure — callers should map this to
    a ``401 Unauthorized``.
    """
    payload = jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
    )
    if payload.get("type") != expected_type:
        raise JWTError(f"Expected token type {expected_type!r}, got {payload.get('type')!r}")
    if "sub" not in payload:
        raise JWTError("Token missing 'sub' claim")
    return payload
