"""Celery task package — distributed async processing for ANO Studio."""

from app.tasks.celery_app import celery_app

__all__ = ["celery_app"]
