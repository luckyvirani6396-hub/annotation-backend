"""Authentication & authorization layer.

Public surface:

* :data:`router`                — FastAPI router mounted at ``/api/auth``
* :func:`get_current_user`      — dependency yielding the authenticated user
* :func:`get_current_active_user` — same, but rejects disabled accounts
"""

from __future__ import annotations

from app.auth.dependencies import (
    get_current_active_user,
    get_current_user,
    oauth2_scheme,
)
from app.auth.router import router

__all__ = [
    "router",
    "get_current_user",
    "get_current_active_user",
    "oauth2_scheme",
]
