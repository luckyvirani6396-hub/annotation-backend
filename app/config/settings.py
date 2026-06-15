from pydantic_settings import BaseSettings
from typing import Optional
import os
from pathlib import Path

class Settings(BaseSettings):
    # Application
    APP_NAME: str = "Annotation Tool"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 9000
    
    FLOWER_BASIC_AUTH: str = "admin:flower"
    REACT_APP_API_URL: str = "http://192.168.5.202:9000"
    # MongoDB
    MONGODB_URL: str = "mongodb://192.168.5.202:27017"
    DATABASE_NAME: str = "annotation_db"
    
    # File Upload — support bulk uploads up to 100,000 images
    MAX_UPLOAD_SIZE: int = 53687091200   # 50 GB total per request
    MAX_BATCH_FILES: int = 100000        # max files per multipart request
    ALLOWED_EXTENSIONS: str = ".jpg,.jpeg,.png,.bmp,.webp"
    # Use absolute path for uploads
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    UPLOAD_DIR: str = str(BASE_DIR / "uploads")

    # Concurrency — parallel I/O workers for upload/export
    # Override via env (e.g. UPLOAD_IO_WORKERS=32) to tune for your hardware.
    UPLOAD_IO_WORKERS: int = min(64, (os.cpu_count() or 4) * 8)
    UPLOAD_BATCH_SIZE: int = 500         # images processed per chunk in bulk uploads
    
    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = str(BASE_DIR / "logs")

    # ── Authentication / JWT ────────────────────────────────────────────────
    # NOTE: override JWT_SECRET in production via env / .env
    JWT_SECRET: str = "change-me-to-a-long-random-string-min-32-chars-please"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TTL_MINUTES: int = 120          # 2h access tokens
    JWT_REFRESH_TTL_DAYS: int = 30            # 30d refresh tokens

    # CORS (comma-separated origins; "*" = allow all, dev only)
    CORS_ORIGINS: str = "*"

    # ── Bootstrap admin (seeded on first boot if no user has this email) ───
    ADMIN_EMAIL: str = "admin@annotation.studio"
    ADMIN_PASSWORD: str = "Admin@2026!"
    ADMIN_FULL_NAME: str = "Workspace Admin"

    # ── Redis / Celery (distributed task queue) ─────────────────────────────
    # In Docker the host is `redis`; on a local dev machine it is `192.168.5.202`.
    REDIS_URL: str = "redis://192.168.5.202:6379/0"
    CELERY_BROKER_URL: str = "redis://192.168.5.202:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://192.168.5.202:6379/2"
    CELERY_TASK_DEFAULT_QUEUE: str = "annostudio"
    # Worker tuning — override per environment.
    CELERY_WORKER_CONCURRENCY: int = max(2, (os.cpu_count() or 4))
    CELERY_WORKER_PREFETCH: int = 4
    # How long completed job records live in Redis (seconds).
    JOB_TTL_SECONDS: int = 60 * 60 * 24 * 3  # 3 days

    # ── Staging area for async uploads ──────────────────────────────────────
    # Incoming bytes are streamed here, then moved to UPLOAD_DIR by the worker.
    STAGING_DIR: str = str(BASE_DIR / "uploads" / "_staging")
    EXPORT_DIR: str = str(BASE_DIR / "uploads" / "_exports")

    # When export runs on a machine that shares MongoDB but not the upload volume,
    # fetch missing images over HTTP from this base URL (e.g. http://192.168.5.202:9000).
    ASSET_BASE_URL: Optional[str] = None

    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()