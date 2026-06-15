# syntax=docker/dockerfile:1.6
# =============================================================================
# ANO Studio — FastAPI backend (also used for the Celery worker image)
# =============================================================================
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps: PIL needs libjpeg/zlib at runtime; build-essential for any
# wheel-less packages that fall back to source.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg62-turbo \
        zlib1g \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache pip layer
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Application code
COPY app ./app

# Runtime dirs (mounted as volumes in production)
RUN mkdir -p /app/uploads/images /app/uploads/_staging /app/uploads/_exports /app/logs

EXPOSE 9000

# Default command — overridden for the worker service in docker-compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000", "--workers", "4"]
