"""Annotation Tool — FastAPI backend.

Top-level package for the annotation service.  Sub-packages:

* :mod:`app.api`        — FastAPI routers (upload, annotations, export)
* :mod:`app.config`     — settings, database, logging configuration
* :mod:`app.middleware` — request/response middleware
* :mod:`app.schemas`    — Pydantic models and document schemas
* :mod:`app.services`   — business-logic layer
* :mod:`app.utils`      — pure helpers (logger, validators, parsers)
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "1.0.0"
