"""HTTP API routers exposed by the FastAPI application."""

# NOTE: do not add `from __future__ import annotations` here -- the submodule
# `annotations` (app/api/annotations.py) collides with that future-feature
# name and ends up shadowed by a `_Feature` object, breaking
# `from app.api import annotations`.

from app.api import annotations, export, upload, admin, task_batches, reviews, progress, categories, jobs, datasets

__all__ = ["annotations", "export", "upload", "admin", "task_batches", "reviews", "progress", "categories", "jobs", "datasets"]
