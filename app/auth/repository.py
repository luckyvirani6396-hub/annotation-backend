"""Persistence helpers for the ``users`` collection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId
from loguru import logger

from pymongo.errors import OperationFailure

from app.config.database import get_collection

_COLLECTION = "users"


# ─── indexes ─────────────────────────────────────────────────────────────────


async def ensure_user_indexes() -> None:
    """Create the unique-email index.  Idempotent.

    Silently skips code-85 ``IndexOptionsConflict`` so the server starts
    cleanly even when the index already exists under a different name (e.g.
    the auto-generated ``email_1`` created by ``init_rbac_collections``).
    """
    coll = get_collection(_COLLECTION)
    try:
        await coll.create_index("email", unique=True, name="uniq_email")
        logger.info("Ensured unique index on users.email")
    except OperationFailure as exc:
        if exc.code == 85:  # IndexOptionsConflict — already exists, different name
            logger.debug("users.email index already exists (skipping): {}", exc.details)
        else:
            raise


# ─── serialisation ───────────────────────────────────────────────────────────


def _serialise(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert ``ObjectId`` to ``str`` so Pydantic can validate the doc."""
    doc = dict(doc)
    if isinstance(doc.get("_id"), ObjectId):
        doc["_id"] = str(doc["_id"])
    return doc


# ─── queries ─────────────────────────────────────────────────────────────────


async def get_user_by_email(email: str) -> Optional[dict[str, Any]]:
    coll = get_collection(_COLLECTION)
    doc = await coll.find_one({"email": email.lower()})
    return _serialise(doc) if doc else None


async def get_user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    coll = get_collection(_COLLECTION)
    try:
        oid = ObjectId(user_id)
    except Exception:
        return None
    doc = await coll.find_one({"_id": oid})
    return _serialise(doc) if doc else None


async def create_user(
    *,
    email: str,
    full_name: str,
    password_hash: str,
    role: str = "annotator",
    department: Optional[str] = None,
) -> dict[str, Any]:
    coll = get_collection(_COLLECTION)
    now = datetime.now(tz=timezone.utc)
    doc = {
        "email": email.lower(),
        "full_name": full_name.strip(),
        "password_hash": password_hash,
        "is_active": True,
        "role": role,
        "department": department,
        "created_at": now,
        "updated_at": now,
        "last_login_at": None,
    }
    result = await coll.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc


async def touch_last_login(user_id: str) -> None:
    coll = get_collection(_COLLECTION)
    try:
        oid = ObjectId(user_id)
    except Exception:
        return
    await coll.update_one(
        {"_id": oid},
        {"$set": {"last_login_at": datetime.now(tz=timezone.utc)}},
    )


async def update_user_profile(user_id: str, *, full_name: str) -> Optional[dict[str, Any]]:
    """Patch the editable profile fields and return the fresh document."""
    coll = get_collection(_COLLECTION)
    try:
        oid = ObjectId(user_id)
    except Exception:
        return None
    now = datetime.now(tz=timezone.utc)
    doc = await coll.find_one_and_update(
        {"_id": oid},
        {"$set": {"full_name": full_name.strip(), "updated_at": now}},
        return_document=True,
    )
    # PyMongo's ``find_one_and_update`` returns the *pre*-update doc by default
    # in older versions; explicitly re-fetch to be safe across drivers.
    fresh = await coll.find_one({"_id": oid})
    return _serialise(fresh) if fresh else None


async def update_user_password(user_id: str, *, password_hash: str) -> bool:
    """Persist a new password hash and stamp ``password_changed_at``."""
    coll = get_collection(_COLLECTION)
    try:
        oid = ObjectId(user_id)
    except Exception:
        return False
    now = datetime.now(tz=timezone.utc)
    res = await coll.update_one(
        {"_id": oid},
        {
            "$set": {
                "password_hash": password_hash,
                "password_changed_at": now,
                "updated_at": now,
            }
        },
    )
    return res.modified_count == 1


async def delete_user(user_id: str) -> bool:
    """Hard-delete the user record. Returns True if a record was removed."""
    coll = get_collection(_COLLECTION)
    try:
        oid = ObjectId(user_id)
    except Exception:
        return False
    res = await coll.delete_one({"_id": oid})
    return res.deleted_count == 1


# ─── bootstrap ───────────────────────────────────────────────────────────────


async def seed_admin_user(
    *, email: str, password: str, full_name: str
) -> None:
    """Create the bootstrap admin account on first boot.

    Idempotent: if a user with the same email already exists the function is
    a no-op.  Imported lazily to avoid a circular dependency with the
    ``security`` module.
    """
    from app.auth.security import hash_password  # local import

    existing = await get_user_by_email(email)
    if existing is not None:
        return

    await create_user(
        email=email,
        full_name=full_name,
        password_hash=hash_password(password),
        role="admin",
    )
    logger.info("Seeded bootstrap admin user '{}'.", email.lower())
