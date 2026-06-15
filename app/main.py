import os
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ── Raise Starlette's multipart limits so bulk uploads of up to 10 000
#    images succeed (default is only 1 000 files / 1 000 form fields). ──
from starlette.formparsers import MultiPartParser
from starlette.requests import Request as _StarletteRequest

MultiPartParser.max_files = 200000
MultiPartParser.max_fields = 200000

# FastAPI's File/Form dependencies call ``request.form()`` without kwargs,
# which uses Starlette's hard-coded defaults of 1000.  Override the bound
# method so every request inherits the higher limits transparently.
_orig_form = _StarletteRequest.form
def _form_with_high_limits(self, *, max_files=200000, max_fields=200000):
    return _orig_form(self, max_files=max_files, max_fields=max_fields)
_StarletteRequest.form = _form_with_high_limits

from app.config.database import init_db, close_db, db_manager
from app.config.settings import settings
from app.config.logging_config import setup_logging
from app.config.rbac_init import init_rbac_collections, init_enhanced_users_schema
from app.api import annotations, upload, export, jobs, categories, admin, task_batches, reviews, progress, datasets
from app.auth import router as auth_router
from app.auth.repository import ensure_user_indexes, seed_admin_user
from app.middleware.logging import LoggingMiddleware
from app.middleware.error_handler import ErrorHandlerMiddleware

# Setup logging
setup_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    # Startup
    await init_db()
    db = db_manager.get_db()
    
    # Initialize RBAC collections and schemas
    await init_rbac_collections(db)
    await init_enhanced_users_schema(db)
    
    await ensure_user_indexes()
    await seed_admin_user(
        email=settings.ADMIN_EMAIL,
        password=settings.ADMIN_PASSWORD,
        full_name=settings.ADMIN_FULL_NAME,
    )
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    os.makedirs(settings.STAGING_DIR, exist_ok=True)
    os.makedirs(settings.EXPORT_DIR, exist_ok=True)
    yield
    # Shutdown
    await close_db()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Middleware
_cors_is_wildcard = settings.CORS_ORIGINS.strip() == "*"
_cors_origins = (
    ["*"] if _cors_is_wildcard
    else [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
)
# Browsers reject allow_origins=["*"] together with allow_credentials=True.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=not _cors_is_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
app.add_middleware(LoggingMiddleware)
app.add_middleware(ErrorHandlerMiddleware)

# Static files
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")

# Routes
# /api/auth/* is intentionally NOT protected — these endpoints establish
# the session.  All other routers require a valid bearer token.
from app.auth.dependencies import get_current_active_user  # noqa: E402

_protected = [Depends(get_current_active_user)]

app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(admin.router, tags=["admin_create_users"], dependencies=_protected)
app.include_router(upload.router, prefix="/api/upload", tags=["Upload"], dependencies=_protected)
app.include_router(annotations.router, prefix="/api/annotations", tags=["Annotations"], dependencies=_protected)
app.include_router(categories.router, prefix="/api/annotations", tags=["Categories"], dependencies=_protected)
app.include_router(task_batches.router, tags=["Task Batches"], dependencies=_protected)
app.include_router(datasets.router, tags=["Datasets"], dependencies=_protected)
app.include_router(reviews.router, tags=["Reviews"], dependencies=_protected)
app.include_router(progress.router, tags=["Progress"], dependencies=_protected)
app.include_router(export.router, prefix="/api/export", tags=["Export"], dependencies=_protected)
app.include_router(jobs.router, prefix="/api/jobs", tags=["Jobs"], dependencies=_protected)

@app.get("/")
async def root():
    return {"message": f"Welcome to {settings.APP_NAME}", "version": settings.APP_VERSION}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}