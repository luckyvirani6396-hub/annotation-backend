"""Application configuration: settings, database, logging."""

from __future__ import annotations

from app.config.database import close_db, get_collection, get_database, init_db
from app.config.logging_config import setup_logging
from app.config.settings import Settings, settings

__all__ = [
    "Settings",
    "settings",
    "setup_logging",
    "init_db",
    "close_db",
    "get_database",
    "get_collection",
]
