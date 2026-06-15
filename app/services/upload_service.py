"""
Upload Service — high-throughput image ingestion with parallel disk writes.

Architecture
------------
*  Step 1 — Async reads: file contents are read one-at-a-time via ``await
   file.read()``.  Multipart parsers are not thread-safe, so reads stay on
   the event loop.

*  Step 2 — Parallel writes: once every file's bytes are in memory, all
   write + PIL-probe tasks are submitted to a ``ThreadPoolExecutor`` and
   awaited concurrently via ``asyncio.gather``.  On a typical SSD this
   cuts per-file latency by ~IO_WORKERS× compared to a sequential loop.

*  Batch chunking: for very large uploads (20 k–100 k files) the batch is
   split into ``UPLOAD_BATCH_SIZE`` chunks so memory usage stays bounded
   and progress can be tracked per-chunk.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from fastapi import UploadFile
from loguru import logger
from PIL import Image as PILImage

from app.config.settings import settings

# ---------------------------------------------------------------------------
# Worker-pool sizing (module-level so the pool is reused across requests)
# ---------------------------------------------------------------------------
_IO_WORKERS: int = min(64, (os.cpu_count() or 4) * 8)
_io_executor = ThreadPoolExecutor(max_workers=_IO_WORKERS, thread_name_prefix="upload-io")

# Max files processed in a single chunk when handling very large batches.
# Keeps peak memory usage at roughly UPLOAD_BATCH_SIZE × avg_image_size.
UPLOAD_BATCH_SIZE: int = 500


# ---------------------------------------------------------------------------
# Module-level worker (plain function → picklable / executor-safe)
# ---------------------------------------------------------------------------
def _write_and_probe(
    args: Tuple[str, str, bytes],
) -> Tuple[str, int, int]:
    """Write image bytes to disk and return ``(file_path, width, height)``.

    Parameters
    ----------
    args:
        ``(absolute_file_path, lowercase_extension, file_bytes)``
    """
    file_path, ext, content = args
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "wb") as fh:
        fh.write(content)

    width = height = 0
    if ext in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        try:
            with PILImage.open(io.BytesIO(content)) as img:
                width, height = img.size
        except Exception:
            pass  # dimensions are optional — fall back to 0×0

    return file_path, width, height


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class UploadService:

    # ------------------------------------------------------------------
    # Single-file helper (used by /single endpoint)
    # ------------------------------------------------------------------
    @staticmethod
    async def save_upload_file(
        file: UploadFile,
        subfolder: str = "",
    ) -> Tuple[str, str, int, int, int]:
        """Save one ``UploadFile`` and return ``(stored_name, path, size, w, h)``."""
        ext = os.path.splitext(file.filename)[1].lower()
        stored_name = f"{uuid.uuid4().hex}{ext}"
        upload_dir = os.path.join(settings.UPLOAD_DIR, subfolder)
        os.makedirs(upload_dir, exist_ok=True)
        file_path = os.path.join(upload_dir, stored_name)

        content = await file.read()
        file_size = len(content)

        loop = asyncio.get_event_loop()
        _, width, height = await loop.run_in_executor(
            _io_executor, _write_and_probe, (file_path, ext, content)
        )

        logger.info(f"[upload] saved {stored_name} ({file_size:,} bytes, {width}×{height})")
        return stored_name, file_path, file_size, width, height

    # ------------------------------------------------------------------
    # Batch helper (used by bulk-upload endpoint)
    # ------------------------------------------------------------------
    @staticmethod
    async def save_multiple_images(
        files: List[UploadFile],
    ) -> Tuple[List[dict], List[str]]:
        """Save a batch of images with parallel disk writes.

        Handles up to 100 k files by processing them in chunks of
        ``UPLOAD_BATCH_SIZE`` so memory stays bounded.

        Returns
        -------
        ``(uploaded_list, failed_list)``
        Each entry in *uploaded_list* is a dict with keys:
        ``original_filename``, ``stored_filename``, ``file_path``,
        ``file_size``, ``width``, ``height``.
        """
        allowed: set = {e.strip() for e in settings.ALLOWED_EXTENSIONS.split(",")}
        upload_dir = os.path.join(settings.UPLOAD_DIR, "images")
        os.makedirs(upload_dir, exist_ok=True)

        all_uploaded: List[dict] = []
        all_failed: List[str] = []

        # Split into manageable chunks
        chunks = [
            files[i : i + UPLOAD_BATCH_SIZE]
            for i in range(0, len(files), UPLOAD_BATCH_SIZE)
        ]
        logger.info(
            f"[upload] {len(files)} files → {len(chunks)} chunk(s) of ≤{UPLOAD_BATCH_SIZE}"
        )

        for chunk_idx, chunk in enumerate(chunks, start=1):
            # ── Step 1: async reads (event-loop safe) ──────────────────
            pending: List[Tuple[str, str, bytes]] = []  # (original, stored, content)
            for file in chunk:
                if not file.filename:
                    continue
                ext = os.path.splitext(file.filename)[1].lower()
                if ext not in allowed:
                    all_failed.append(f"{file.filename} — unsupported extension '{ext}'")
                    continue
                try:
                    content = await file.read()
                    stored_name = f"{uuid.uuid4().hex}{ext}"
                    pending.append((file.filename, stored_name, content))
                except Exception as exc:
                    logger.error(f"[upload] read failed for {file.filename}: {exc}")
                    all_failed.append(file.filename)

            if not pending:
                continue

            # ── Step 2: parallel writes via asyncio.gather ─────────────
            loop = asyncio.get_event_loop()

            async def _save_one(original: str, stored: str, content: bytes) -> Optional[dict]:
                ext = os.path.splitext(stored)[1].lower()
                file_path = os.path.join(upload_dir, stored)
                try:
                    fp, w, h = await loop.run_in_executor(
                        _io_executor,
                        _write_and_probe,
                        (file_path, ext, content),
                    )
                    return {
                        "original_filename": original,
                        "stored_filename": stored,
                        "file_path": fp,
                        "file_size": len(content),
                        "width": w,
                        "height": h,
                    }
                except Exception as exc:
                    logger.error(f"[upload] write failed for {original}: {exc}")
                    return None

            results = await asyncio.gather(
                *[_save_one(orig, stored, content) for orig, stored, content in pending]
            )

            for orig, result in zip([p[0] for p in pending], results):
                if result is not None:
                    all_uploaded.append(result)
                else:
                    all_failed.append(orig)

            logger.info(
                f"[upload] chunk {chunk_idx}/{len(chunks)}: "
                f"{sum(1 for r in results if r)} saved, "
                f"{sum(1 for r in results if not r)} failed"
            )

        logger.success(
            f"[upload] batch complete — {len(all_uploaded)} saved, "
            f"{len(all_failed)} failed (total submitted: {len(files)})"
        )
        return all_uploaded, all_failed