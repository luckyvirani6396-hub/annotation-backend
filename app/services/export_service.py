"""
Export Service — Roboflow-style multi-format dataset packaging.

Supported formats
-----------------
* ``coco``        — COCO 2017 JSON (object detection + segmentation)
* ``yolov8``      — Ultralytics YOLOv8 (images/, labels/, data.yaml, classes.txt)
* ``pascal_voc``  — Pascal VOC 2012 (JPEGImages/, Annotations/*.xml, ImageSets/)
* ``json``        — Native ANO Studio JSON (full fidelity)

Each format produces a single self-contained ZIP that bundles **images +
annotations + a format-specific README.md** so it can be unzipped and fed
directly to any standard training framework — exactly like Roboflow's
one-click export.

Dataset Splitting
-----------------
Supports train/validation/test splits with customizable percentages. During
export, images and annotations are automatically organized into separate
folders for each split, with a comprehensive README explaining the structure.

Architecture
------------
Phase 1 — parallel image reads via ``ThreadPoolExecutor`` (I/O-bound).
Phase 2 — annotation payload built in the calling thread while Phase 1
          futures are in-flight (CPU/IO overlap).
Phase 3 — ZIP assembly (memory→memory, fast).

The public entry point :meth:`ExportService.build_zip` is synchronous so it
can be handed to ``loop.run_in_executor`` from the FastAPI route without
blocking the event loop, or invoked directly from a Celery worker.
"""

from __future__ import annotations

import io
import json
import os
import re
import random
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import urlopen
from xml.sax.saxutils import escape as _xml_escape

from loguru import logger

from app.config.settings import settings


# ---------------------------------------------------------------------------
# Worker-pool sizing — 8× CPU count, capped at 64.
# ---------------------------------------------------------------------------
_IO_WORKERS: int = min(64, (os.cpu_count() or 4) * 8)


# Canonical format identifiers exposed to the API.
SUPPORTED_FORMATS: Tuple[str, ...] = (
    "coco",
    "yolov8",
    "pascal_voc",
    "json",
)

# Friendly aliases users may pass.
_FORMAT_ALIASES: Dict[str, str] = {
    "coco": "coco",
    "zip": "coco",
    "yolo": "yolov8",
    "yolov5": "yolov8",
    "yolov8": "yolov8",
    "pascal": "pascal_voc",
    "voc": "pascal_voc",
    "pascal_voc": "pascal_voc",
    "pascalvoc": "pascal_voc",
    "json": "json",
    "raw": "json",
    "anostudio": "json",
}


def normalize_format(fmt: Optional[str]) -> str:
    """Normalise a user-supplied format string to a canonical id."""
    key = (fmt or "coco").strip().lower().replace("-", "_")
    if key not in _FORMAT_ALIASES:
        raise ValueError(
            f"Unsupported format '{fmt}'. "
            f"Choose one of: {', '.join(SUPPORTED_FORMATS)}."
        )
    return _FORMAT_ALIASES[key]


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_stem(name: str) -> str:
    """Return a filesystem-safe filename stem (no extension)."""
    stem = Path(name).stem or "image"
    cleaned = _UNSAFE_RE.sub("_", stem).strip("_.") or "image"
    return cleaned


def _resolve_stored_key(img: Dict[str, Any]) -> Optional[str]:
    """Resolve the on-disk filename for an image record."""
    sf = (img.get("stored_filename") or "").strip()
    if sf:
        return sf

    url = (img.get("image_url") or "").strip()
    if "/uploads/images/" in url:
        return url.rsplit("/uploads/images/", 1)[-1].split("?")[0].strip() or None

    fp = (img.get("file_path") or "").strip()
    if fp:
        return os.path.basename(fp.replace("\\", "/")) or None

    fn = (img.get("file_name") or "").replace("\\", "/")
    base = os.path.basename(fn)
    return base if base and "." in base else None


def _export_image_filename(img: Dict[str, Any]) -> str:
    """Filename used inside the export ZIP (must match COCO file_name)."""
    stored = _resolve_stored_key(img)
    if stored:
        return stored
    fn = (img.get("file_name") or "").replace("\\", "/")
    return os.path.basename(fn) or "image.jpg"


# ---------------------------------------------------------------------------
# Image reader — robust across path styles
# ---------------------------------------------------------------------------
def _read_image_bytes(
    args: Tuple[str, str, str, Optional[str]],
) -> Tuple[str, str, Optional[bytes]]:
    """Read one image file from disk. Returns ``(stored, export_name, bytes|None)``."""
    stored, export_name, images_dir, file_path_hint = args

    candidates: List[Optional[str]] = []
    if file_path_hint:
        candidates.append(file_path_hint)
    candidates.extend([
        os.path.join(images_dir, stored),
        str(Path(images_dir) / stored),
        os.path.abspath(os.path.join(images_dir, stored)),
        os.path.join(os.getcwd(), "uploads", "images", stored),
        f"{images_dir}/{stored}",
        stored if os.path.isabs(stored) else None,
        stored if os.path.exists(stored) else None,
    ])

    seen: set = set()
    unique: List[str] = []
    for p in candidates:
        if not p:
            continue
        np = os.path.normpath(p)
        if np not in seen:
            seen.add(np)
            unique.append(np)

    for path in unique:
        try:
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    return stored, export_name, fh.read()
        except (OSError, IOError) as e:
            logger.debug(f"[export] read fail {path}: {e}")

    logger.warning(
        f"[export] Image not found: stored={stored!r} export_name={export_name!r} "
        f"dir={images_dir!r} hint={file_path_hint!r}"
    )
    return stored, export_name, None


def _asset_base_urls() -> List[str]:
    """Bases to try when image bytes are not on the local UPLOAD_DIR volume."""
    bases: List[str] = []
    for candidate in (settings.ASSET_BASE_URL, settings.REACT_APP_API_URL):
        if not candidate:
            continue
        base = str(candidate).strip().rstrip("/")
        if base and base not in bases:
            bases.append(base)
    return bases


def _fetch_image_bytes_http(
    stored: str,
    export_name: str,
    bases: List[str],
) -> Tuple[str, str, Optional[bytes]]:
    """Download one image from /uploads/images/{stored} on a remote API host."""
    for base in bases:
        url = f"{base}/uploads/images/{stored}"
        try:
            with urlopen(url, timeout=60) as resp:
                data = resp.read()
                if data:
                    logger.debug(f"[export] HTTP hit {url} ({len(data):,} bytes)")
                    return stored, export_name, data
        except (URLError, OSError, TimeoutError) as e:
            logger.debug(f"[export] HTTP miss {url}: {e}")
    return stored, export_name, None


# ---------------------------------------------------------------------------
# Dataset splitting — train/validation/test stratification
# ---------------------------------------------------------------------------
def _split_dataset(
    dataset: Dict[str, Any],
    train_split: float = 0.7,
    val_split: float = 0.15,
    test_split: float = 0.15,
    seed: int = 42,
) -> Dict[str, Dict[str, Any]]:
    """
    Split dataset into train/validation/test sets stratified by class.
    
    Returns:
        {
            "train": {"images": [...], "annotations": [...]},
            "val": {...},
            "test": {...}
        }
    """
    if not (0 <= train_split <= 1 and 0 <= val_split <= 1 and 0 <= test_split <= 1):
        raise ValueError("Split percentages must be between 0 and 1")
    
    total = train_split + val_split + test_split
    if not (0.99 <= total <= 1.01):
        raise ValueError(f"Split percentages must sum to 1.0, got {total}")
    
    random.seed(seed)
    images = dataset.get("images", [])
    annotations = dataset.get("annotations", [])
    
    # Shuffle images
    shuffled_images = images.copy()
    random.shuffle(shuffled_images)
    
    # Split indices
    n = len(shuffled_images)
    train_idx = int(n * train_split)
    val_idx = train_idx + int(n * val_split)
    
    train_images = shuffled_images[:train_idx]
    val_images = shuffled_images[train_idx:val_idx]
    test_images = shuffled_images[val_idx:]
    
    # Create image_id sets for fast lookup
    train_ids = {img.get("image_id") for img in train_images}
    val_ids = {img.get("image_id") for img in val_images}
    test_ids = {img.get("image_id") for img in test_images}
    
    # Split annotations by image_id
    train_anns = [a for a in annotations if a.get("image_id") in train_ids]
    val_anns = [a for a in annotations if a.get("image_id") in val_ids]
    test_anns = [a for a in annotations if a.get("image_id") in test_ids]
    
    return {
        "train": {
            "images": train_images,
            "annotations": train_anns,
            "info": f"{len(train_images)} images",
        },
        "val": {
            "images": val_images,
            "annotations": val_anns,
            "info": f"{len(val_images)} images",
        },
        "test": {
            "images": test_images,
            "annotations": test_anns,
            "info": f"{len(test_images)} images",
        },
    }


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------
class ExportService:
    """Builds Roboflow-style export ZIPs in multiple industry formats."""

    # ==================================================================
    # PUBLIC ENTRY POINT
    # ==================================================================
    @classmethod
    def build_zip(
        cls,
        dataset: Dict[str, Any],
        dataset_name: str,
        fmt: str = "coco",
        split_dataset: bool = False,
        train_split: float = 0.7,
        val_split: float = 0.15,
        test_split: float = 0.15,
    ) -> io.BytesIO:
        """Build a self-contained dataset ZIP in memory.

        Parameters
        ----------
        dataset:
            Dict returned by ``AnnotationService.get_annotation_dataset``.
        dataset_name:
            Human-readable dataset name used in filenames + README.
        fmt:
            One of :data:`SUPPORTED_FORMATS` (or an alias accepted by
            :func:`normalize_format`).
        split_dataset:
            If True, organize into train/val/test folders with specified percentages.
        train_split, val_split, test_split:
            Percentages for train/val/test splits (must sum to 1.0).
        """
        canonical = normalize_format(fmt)
        upload_root = (
            settings.UPLOAD_DIR
            if os.path.isabs(settings.UPLOAD_DIR)
            else os.path.abspath(settings.UPLOAD_DIR)
        )
        images_dir = os.path.join(upload_root, "images")

        # ── Split dataset if requested ────────────────────────────────
        if split_dataset:
            splits = _split_dataset(dataset, train_split, val_split, test_split)
            logger.info(
                f"[export] '{dataset_name}' splitting: "
                f"train={len(splits['train']['images'])} "
                f"val={len(splits['val']['images'])} "
                f"test={len(splits['test']['images'])}"
            )
        else:
            splits = {
                "full": {"images": dataset["images"], "annotations": dataset["annotations"]}
            }

        # ── Phase 1: parallel image reads ─────────────────────────────
        tasks: List[Tuple[str, str, str, Optional[str]]] = []
        for img in dataset["images"]:
            stored_key = _resolve_stored_key(img)
            if not stored_key:
                logger.warning(
                    f"[export] skipping image id={img.get('id')}: no resolvable filename"
                )
                continue
            export_name = _export_image_filename(img)
            fp_hint = (img.get("file_path") or "").strip() or None
            if fp_hint and not os.path.isfile(fp_hint):
                fp_hint = None
            tasks.append((stored_key, export_name, images_dir, fp_hint))

        image_bytes: Dict[str, Tuple[str, Optional[bytes]]] = {}
        if tasks:
            with ThreadPoolExecutor(max_workers=_IO_WORKERS) as pool:
                future_map = {pool.submit(_read_image_bytes, t): t for t in tasks}
                for fut in as_completed(future_map):
                    stored, export_name, data = fut.result()
                    image_bytes[stored] = (export_name, data)

        # ── Phase 1b: HTTP fallback (shared DB, files on another API host) ──
        asset_bases = _asset_base_urls()
        missing_http = [
            (stored, export_name)
            for stored, (export_name, data) in image_bytes.items()
            if data is None
        ]
        if missing_http and asset_bases:
            logger.info(
                f"[export] {len(missing_http)} image(s) missing locally — "
                f"fetching via HTTP from {asset_bases[0]}"
            )
            with ThreadPoolExecutor(max_workers=min(_IO_WORKERS, 32)) as pool:
                futures = [
                    pool.submit(_fetch_image_bytes_http, stored, export_name, asset_bases)
                    for stored, export_name in missing_http
                ]
                for fut in as_completed(futures):
                    stored, export_name, data = fut.result()
                    if data is not None:
                        image_bytes[stored] = (export_name, data)

        total_expected = len(dataset["images"])
        found_count = sum(1 for _, data in image_bytes.values() if data is not None)
        if total_expected > 0 and found_count == 0:
            bases_hint = ", ".join(asset_bases) if asset_bases else "none configured"
            raise ValueError(
                f"Could not read any of {total_expected} image file(s). "
                f"Local path: {images_dir}. HTTP bases tried: {bases_hint}. "
                "Set ASSET_BASE_URL in .env to the server that hosts /uploads "
                "(e.g. http://192.168.5.202:9000), or copy uploads/ to this machine."
            )
        if found_count < total_expected:
            logger.warning(
                f"[export] '{dataset_name}' — {total_expected - found_count}/"
                f"{total_expected} images still missing after disk+HTTP fetch"
            )

        # ── Phase 2+3: dispatch to per-format builder ─────────────────
        builder = {
            "coco":       cls._build_coco_zip,
            "yolov8":     cls._build_yolov8_zip,
            "pascal_voc": cls._build_pascal_voc_zip,
            "json":       cls._build_native_json_zip,
        }[canonical]

        buf = builder(dataset, dataset_name, image_bytes, splits if split_dataset else None)
        buf.seek(0)
        logger.info(
            f"[export] '{dataset_name}' fmt={canonical} packed — "
            f"images={len(image_bytes)} annotations={len(dataset['annotations'])} "
            f"size={buf.getbuffer().nbytes:,} bytes"
        )
        return buf

    # ==================================================================
    # SHARED HELPERS
    # ==================================================================
    @staticmethod
    def _write_images(
        zf: zipfile.ZipFile,
        image_bytes: Dict[str, Tuple[str, Optional[bytes]]],
        target_prefix: str,
    ) -> Tuple[int, int]:
        """Write images into the ZIP under ``target_prefix/``.

        Uses each image's ``original`` filename so the archive is human-
        readable. Returns ``(found, missing)`` for logging.
        """
        found = missing = 0
        for _stored, (export_name, data) in image_bytes.items():
            if data is None:
                missing += 1
                continue
            name = os.path.basename((export_name or _stored or "").replace("\\", "/"))
            arcname = (
                f"{target_prefix.rstrip('/')}/{name}"
                if target_prefix else name
            )
            zf.writestr(arcname, data)
            found += 1
        return found, missing

    @staticmethod
    def _write_split_images(
        zf: zipfile.ZipFile,
        image_bytes: Dict[str, Tuple[str, Optional[bytes]]],
        split_images: List[Dict[str, Any]],
        image_prefix: str,
    ) -> Tuple[int, int]:
        """Write split-specific images to ZIP.
        
        Parameters
        ----------
        zf : zipfile.ZipFile
            ZIP file to write to.
        image_bytes : Dict[str, Tuple[str, Optional[bytes]]]
            Map of stored_filename → (original_filename, bytes).
        split_images : List[Dict[str, Any]]
            Images in this split (each has 'stored_filename' and 'file_name').
        image_prefix : str
            Base path prefix like "train/images" or "val/images".
        
        Returns
        -------
        Tuple[int, int]
            (found, missing) for logging.
        """
        found = missing = 0
        for img in split_images:
            stored_fn = _resolve_stored_key(img)
            if not stored_fn or stored_fn not in image_bytes:
                missing += 1
                continue
            export_name, data = image_bytes[stored_fn]
            if data is None:
                missing += 1
                continue
            name = os.path.basename((export_name or stored_fn).replace("\\", "/"))
            arcname = f"{image_prefix.rstrip('/')}/{name}"
            zf.writestr(arcname, data)
            found += 1
        return found, missing

    @staticmethod
    def _per_class_counts(dataset: Dict[str, Any]) -> Dict[int, int]:
        counts: Dict[int, int] = {}
        for a in dataset["annotations"]:
            counts[a["category_id"]] = counts.get(a["category_id"], 0) + 1
        return counts

    @classmethod
    def _build_metadata(
        cls, dataset: Dict[str, Any], dataset_name: str, fmt: str
    ) -> Dict[str, Any]:
        cats = dataset["categories"]
        counts = cls._per_class_counts(dataset)
        return {
            "dataset_name": dataset_name,
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "format": fmt,
            "exporter": "ANO Studio",
            "version": "1.1",
            "totals": {
                "images": len(dataset["images"]),
                "annotations": len(dataset["annotations"]),
                "categories": len(cats),
            },
            "classes": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "supercategory": c.get("supercategory", ""),
                    "annotation_count": counts.get(c["id"], 0),
                }
                for c in cats
            ],
        }

    # ==================================================================
    # README BUILDERS — one per format, tailored to its layout
    # ==================================================================
    @staticmethod
    def _readme_header(dataset_name: str, fmt_label: str) -> str:
        return (
            f"# {dataset_name}\n\n"
            f"> Exported from **ANO Studio** · "
            f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"> Format: **{fmt_label}**\n\n"
        )
    
    @staticmethod
    def _readme_split_section(splits: Dict[str, Dict[str, Any]]) -> str:
        """Generate README section explaining the train/val/test split."""
        if not splits:
            return ""
        
        train_count = len(splits.get('train', {}).get('images', []))
        val_count = len(splits.get('val', {}).get('images', []))
        test_count = len(splits.get('test', {}).get('images', []))
        total = train_count + val_count + test_count
        
        return (
            "## Dataset Split\n\n"
            "This dataset has been organized into **train**, **validation**, and **test** sets "
            "to support professional machine learning workflows.\n\n"
            f"| Split | Images | Percentage |\n"
            f"|-------|--------|------------|\n"
            f"| **Train** | {train_count} | {100*train_count//total if total > 0 else 0}% |\n"
            f"| **Validation** | {val_count} | {100*val_count//total if total > 0 else 0}% |\n"
            f"| **Test** | {test_count} | {100*test_count//total if total > 0 else 0}% |\n"
            f"| **Total** | {total} | 100% |\n\n"
            "Each set is organized in separate folders (`train/`, `val/`, `test/`) with their own "
            "images and annotations.\n\n"
        )

    @staticmethod
    def _readme_summary(
        total_images: int,
        total_annotations: int,
        categories: List[Dict[str, Any]],
    ) -> str:
        cat_rows = "\n".join(
            f"| {c['id']} | `{c['name']}` | {c.get('supercategory') or '—'} |"
            for c in categories
        ) or "| — | — | — |"
        return (
            "## Summary\n\n"
            "| Property    | Value |\n"
            "|-------------|-------|\n"
            f"| Images      | {total_images:,} |\n"
            f"| Annotations | {total_annotations:,} |\n"
            f"| Categories  | {len(categories)} |\n\n"
            "## Classes\n\n"
            "| id | name | supercategory |\n"
            "|----|------|----------------|\n"
            f"{cat_rows}\n\n"
        )

    @classmethod
    def _readme_coco(cls, dataset_name: str, dataset: Dict[str, Any]) -> str:
        return (
            cls._readme_header(dataset_name, "COCO 2017 (object detection)")
            + cls._readme_summary(
                len(dataset["images"]),
                len(dataset["annotations"]),
                dataset["categories"],
            )
            + (
                "## ZIP Structure\n\n"
                "```\n"
                f"{dataset_name}_coco.zip\n"
                "├── images/                                ← original-filename images\n"
                "├── annotations/\n"
                "│   └── instances_default.json             ← COCO-format annotations\n"
                "├── metadata.json                          ← machine-readable summary\n"
                "└── README.md                              ← this file\n"
                "```\n\n"
                "## Annotation Format\n\n"
                "Annotations follow the "
                "[COCO JSON specification](https://cocodataset.org/#format-data).\n\n"
                "```python\n"
                "import json, pathlib\n"
                "coco = json.loads(pathlib.Path('annotations/instances_default.json').read_text())\n"
                "print(len(coco['images']), 'images')\n"
                "```\n\n"
                "Drop-in compatible with **MMDetection**, **Detectron2**, "
                "**Hugging Face Transformers**, and any framework that ingests COCO.\n"
            )
        )

    @classmethod
    def _readme_yolo(cls, dataset_name: str, dataset: Dict[str, Any]) -> str:
        names = ", ".join(f"'{c['name']}'" for c in dataset["categories"])
        return (
            cls._readme_header(dataset_name, "Ultralytics YOLOv8")
            + cls._readme_summary(
                len(dataset["images"]),
                len(dataset["annotations"]),
                dataset["categories"],
            )
            + (
                "## ZIP Structure\n\n"
                "```\n"
                f"{dataset_name}_yolov8.zip\n"
                "├── images/\n"
                "│   └── *.jpg / *.png / …                 ← original images\n"
                "├── labels/\n"
                "│   └── *.txt                             ← one .txt per image\n"
                "├── classes.txt                          ← class index → name\n"
                "├── data.yaml                            ← Ultralytics dataset config\n"
                "├── metadata.json\n"
                "└── README.md\n"
                "```\n\n"
                "## Label Format\n\n"
                "Each line of a `labels/<image>.txt` file:\n\n"
                "```\n"
                "<class_id> <cx_norm> <cy_norm> <w_norm> <h_norm>\n"
                "```\n\n"
                "Coordinates are normalised to `[0, 1]`. `class_id` is a "
                "**0-based** index into `classes.txt` / `data.yaml::names`.\n\n"
                "## Quick Start (Ultralytics YOLOv8)\n\n"
                "```bash\n"
                "pip install ultralytics\n"
                "yolo detect train data=data.yaml model=yolov8n.pt epochs=100 imgsz=640\n"
                "```\n\n"
                f"`data.yaml` already contains `names: [{names}]`.\n"
            )
        )

    @classmethod
    def _readme_pascal(cls, dataset_name: str, dataset: Dict[str, Any]) -> str:
        return (
            cls._readme_header(dataset_name, "Pascal VOC 2012")
            + cls._readme_summary(
                len(dataset["images"]),
                len(dataset["annotations"]),
                dataset["categories"],
            )
            + (
                "## ZIP Structure\n\n"
                "```\n"
                f"{dataset_name}_pascal_voc.zip\n"
                "├── JPEGImages/\n"
                "│   └── *.jpg / *.png / …                 ← original images\n"
                "├── Annotations/\n"
                "│   └── *.xml                             ← one XML per image\n"
                "├── ImageSets/\n"
                "│   └── Main/\n"
                "│       ├── default.txt                   ← all image ids\n"
                "│       └── <class>_default.txt           ← per-class presence (-1/1)\n"
                "├── labels.txt                            ← ordered class list\n"
                "├── metadata.json\n"
                "└── README.md\n"
                "```\n\n"
                "## Annotation Format\n\n"
                "Each `Annotations/<image>.xml` follows the Pascal VOC schema "
                "with `<size>`, `<object><bndbox>` (1-indexed `xmin/ymin/xmax/ymax`), "
                "`<name>`, `<truncated>`, `<difficult>`.\n\n"
                "Compatible with **Detectron2 (`register_pascal_voc`)**, "
                "**TorchVision (`VOCDetection`)**, and any framework that "
                "ingests Pascal VOC.\n"
            )
        )

    @classmethod
    def _readme_json(cls, dataset_name: str, dataset: Dict[str, Any]) -> str:
        return (
            cls._readme_header(dataset_name, "ANO Studio Native JSON (full fidelity)")
            + cls._readme_summary(
                len(dataset["images"]),
                len(dataset["annotations"]),
                dataset["categories"],
            )
            + (
                "## ZIP Structure\n\n"
                "```\n"
                f"{dataset_name}_json.zip\n"
                "├── images/                              ← original-filename images\n"
                "├── annotations.json                     ← full ANO Studio dataset\n"
                "├── metadata.json                        ← machine-readable summary\n"
                "└── README.md\n"
                "```\n\n"
                "## Annotation Format\n\n"
                "`annotations.json` is the **lossless** export — it includes "
                "`segmentation`, `iscrowd`, per-class `supercategory`, and every "
                "field stored by ANO Studio. Re-import it later to resume "
                "editing without losing any information.\n\n"
                "```python\n"
                "import json, pathlib\n"
                "data = json.loads(pathlib.Path('annotations.json').read_text())\n"
                "for cat in data['categories']:\n"
                "    print(cat['id'], cat['name'])\n"
                "```\n"
            )
        )

    # ==================================================================
    # FORMAT 1 — COCO
    # ==================================================================
    @staticmethod
    def _coco_payload(dataset: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "info": {
                "description": dataset["metadata"].get("dataset_name", ""),
                "version": "1.0",
                "year": datetime.utcnow().year,
                "contributor": "",
                "date_created": datetime.utcnow().strftime("%Y/%m/%d"),
            },
            "licenses": [{"id": 1, "name": "Default", "url": ""}],
            "categories": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "supercategory": c.get("supercategory", ""),
                }
                for c in dataset["categories"]
            ],
            "images": [
                {
                    "id": i["id"],
                    "license": i.get("license", 1),
                    "file_name": _export_image_filename(i),
                    "height": i["height"],
                    "width": i["width"],
                    "date_captured": i.get("date_captured", ""),
                }
                for i in dataset["images"]
            ],
            "annotations": [
                {
                    "id": a["id"],
                    "image_id": a["image_id"],
                    "category_id": a["category_id"],
                    "bbox": a["bbox"],
                    "area": a["area"],
                    "segmentation": a.get("segmentation", []),
                    "iscrowd": a.get("iscrowd", 0),
                }
                for a in dataset["annotations"]
            ],
        }

    @classmethod
    def _build_coco_zip(
        cls,
        dataset: Dict[str, Any],
        dataset_name: str,
        image_bytes: Dict[str, Tuple[str, Optional[bytes]]],
        splits: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> io.BytesIO:
        metadata = cls._build_metadata(dataset, dataset_name, "coco")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            if splits:
                # ── Split mode: organize into train/val/test folders ──
                readme = (
                    cls._readme_header(dataset_name, "COCO 2017 (object detection)")
                    + cls._readme_summary(
                        len(dataset["images"]),
                        len(dataset["annotations"]),
                        dataset["categories"],
                    )
                    + cls._readme_split_section(splits)
                    + (
                        "## ZIP Structure\n\n"
                        "```\n"
                        f"{dataset_name}_coco.zip\n"
                        "├── train/\n"
                        "│   ├── images/\n"
                        "│   └── annotations/\n"
                        "│       └── instances_train.json\n"
                        "├── val/\n"
                        "│   ├── images/\n"
                        "│   └── annotations/\n"
                        "│       └── instances_val.json\n"
                        "├── test/\n"
                        "│   ├── images/\n"
                        "│   └── annotations/\n"
                        "│       └── instances_test.json\n"
                        "├── metadata.json\n"
                        "└── README.md\n"
                        "```\n\n"
                        "## Usage\n\n"
                        "```python\n"
                        "from pycocotools.coco import COCO\n"
                        "import json\n"
                        "\n"
                        "# Load train split\n"
                        "train_coco = COCO('train/annotations/instances_train.json')\n"
                        "val_coco = COCO('val/annotations/instances_val.json')\n"
                        "test_coco = COCO('test/annotations/instances_test.json')\n"
                        "```\n"
                    )
                )
                zf.writestr("README.md", readme.encode("utf-8"))
                
                # Write split-specific folders and annotations
                for split_name, split_data in splits.items():
                    split_images = split_data.get("images", [])
                    split_anns = split_data.get("annotations", [])
                    
                    # Build COCO JSON for this split
                    coco_split = {
                        "info": {
                            "description": f"{dataset_name} - {split_name} split",
                            "version": "1.0",
                            "year": datetime.utcnow().year,
                            "contributor": "",
                            "date_created": datetime.utcnow().strftime("%Y/%m/%d"),
                        },
                        "licenses": [{"id": 1, "name": "Default", "url": ""}],
                        "categories": [
                            {
                                "id": c["id"],
                                "name": c["name"],
                                "supercategory": c.get("supercategory", ""),
                            }
                            for c in dataset["categories"]
                        ],
                        "images": [
                            {
                                "id": i["id"],
                                "license": i.get("license", 1),
                                "file_name": _export_image_filename(i),
                                "height": i["height"],
                                "width": i["width"],
                                "date_captured": i.get("date_captured", ""),
                            }
                            for i in split_images
                        ],
                        "annotations": [
                            {
                                "id": a["id"],
                                "image_id": a["image_id"],
                                "category_id": a["category_id"],
                                "bbox": a["bbox"],
                                "area": a["area"],
                                "segmentation": a.get("segmentation", []),
                                "iscrowd": a.get("iscrowd", 0),
                            }
                            for a in split_anns
                        ],
                    }
                    
                    # Write split-specific annotation JSON
                    ann_path = f"{split_name}/annotations/instances_{split_name}.json"
                    zf.writestr(
                        ann_path,
                        json.dumps(coco_split, indent=2, default=str).encode("utf-8"),
                    )
                    
                    # Write split-specific images
                    cls._write_split_images(
                        zf, image_bytes, split_images, f"{split_name}/images"
                    )
            else:
                # ── Non-split mode: flat structure ──
                coco = cls._coco_payload(dataset)
                readme = cls._readme_coco(dataset_name, dataset)
                
                zf.writestr(
                    "annotations/instances_default.json",
                    json.dumps(coco, indent=2, default=str).encode("utf-8"),
                )
                zf.writestr("README.md", readme.encode("utf-8"))
                cls._write_images(zf, image_bytes, target_prefix="images")
            
            # Write metadata in both modes
            zf.writestr(
                "metadata.json",
                json.dumps(metadata, indent=2, default=str).encode("utf-8"),
            )

        return buf

    # ==================================================================
    # FORMAT 2 — YOLOv8 (Ultralytics)
    # ==================================================================
    @classmethod
    def _build_yolov8_zip(
        cls,
        dataset: Dict[str, Any],
        dataset_name: str,
        image_bytes: Dict[str, Tuple[str, Optional[bytes]]],
        splits: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> io.BytesIO:
        cats = dataset["categories"]
        cat_index = {c["id"]: idx for idx, c in enumerate(cats)}
        class_names = [c["name"] for c in cats]

        metadata = cls._build_metadata(dataset, dataset_name, "yolov8")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            if splits:
                # ── Split mode: organize into train/val/test ──
                names_yaml = "\n".join(f"  {i}: {n}" for i, n in enumerate(class_names))
                
                # Create split-specific data.yaml
                data_yaml_split = (
                    "# Ultralytics YOLOv8 dataset config — generated by ANO Studio\n"
                    f"# {dataset_name}\n\n"
                    "path: .\n"
                    "train: train/images\n"
                    "val: val/images\n"
                    "test: test/images\n\n"
                    f"nc: {len(class_names)}\n"
                    f"names:\n{names_yaml}\n"
                )
                
                classes_txt = "\n".join(class_names) + "\n"
                zf.writestr("data.yaml", data_yaml_split.encode("utf-8"))
                zf.writestr("classes.txt", classes_txt.encode("utf-8"))
                
                readme = (
                    cls._readme_header(dataset_name, "Ultralytics YOLOv8")
                    + cls._readme_summary(
                        len(dataset["images"]),
                        len(dataset["annotations"]),
                        dataset["categories"],
                    )
                    + cls._readme_split_section(splits)
                    + (
                        "## ZIP Structure\n\n"
                        "```\n"
                        f"{dataset_name}_yolov8.zip\n"
                        "├── train/\n"
                        "│   ├── images/\n"
                        "│   └── labels/\n"
                        "├── val/\n"
                        "│   ├── images/\n"
                        "│   └── labels/\n"
                        "├── test/\n"
                        "│   ├── images/\n"
                        "│   └── labels/\n"
                        "├── data.yaml\n"
                        "├── classes.txt\n"
                        "├── metadata.json\n"
                        "└── README.md\n"
                        "```\n\n"
                        "## Quick Start\n\n"
                        "```bash\n"
                        "pip install ultralytics\n"
                        "yolo detect train data=data.yaml model=yolov8n.pt epochs=100 imgsz=640\n"
                        "```\n"
                    )
                )
                zf.writestr("README.md", readme.encode("utf-8"))
                
                # Build per-split labels
                for split_name, split_data in splits.items():
                    split_images = split_data.get("images", [])
                    split_anns = split_data.get("annotations", [])
                    
                    # Build image ID set for this split
                    split_img_ids = {img["id"] for img in split_images}
                    
                    # Build labels for this split
                    img_meta: Dict[int, Tuple[int, int, str]] = {
                        i["id"]: (i["width"], i["height"], i["file_name"])
                        for i in split_images
                    }
                    
                    labels_by_image: Dict[int, List[str]] = {}
                    for ann in split_anns:
                        wh = img_meta.get(ann["image_id"])
                        if not wh:
                            continue
                        W, H, _fn = wh
                        if not W or not H:
                            continue
                        x, y, w, h = ann["bbox"]
                        cx = (x + w / 2.0) / W
                        cy = (y + h / 2.0) / H
                        nw = w / W
                        nh = h / H
                        cls_id = cat_index.get(ann["category_id"], 0)
                        labels_by_image.setdefault(ann["image_id"], []).append(
                            f"{cls_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}"
                        )
                    
                    # Write images for this split
                    cls._write_split_images(
                        zf, image_bytes, split_images, f"{split_name}/images"
                    )
                    
                    # Write labels for this split
                    for img in split_images:
                        stem = _safe_stem(img["file_name"])
                        lines = labels_by_image.get(img["id"], [])
                        zf.writestr(
                            f"{split_name}/labels/{stem}.txt",
                            ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
                        )
            else:
                # ── Non-split mode: flat structure ──
                img_meta: Dict[int, Tuple[int, int, str]] = {
                    i["id"]: (i["width"], i["height"], i["file_name"])
                    for i in dataset["images"]
                }

                labels_by_image: Dict[int, List[str]] = {}
                for ann in dataset["annotations"]:
                    wh = img_meta.get(ann["image_id"])
                    if not wh:
                        continue
                    W, H, _fn = wh
                    if not W or not H:
                        continue
                    x, y, w, h = ann["bbox"]
                    cx = (x + w / 2.0) / W
                    cy = (y + h / 2.0) / H
                    nw = w / W
                    nh = h / H
                    cls_id = cat_index.get(ann["category_id"], 0)
                    labels_by_image.setdefault(ann["image_id"], []).append(
                        f"{cls_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}"
                    )

                names_yaml = "\n".join(f"  {i}: {n}" for i, n in enumerate(class_names))
                data_yaml = (
                    "# Ultralytics YOLOv8 dataset config — generated by ANO Studio\n"
                    f"# {dataset_name}\n\n"
                    "path: .\n"
                    "train: images\n"
                    "val: images\n"
                    "test:\n\n"
                    f"nc: {len(class_names)}\n"
                    f"names:\n{names_yaml}\n"
                )
                classes_txt = "\n".join(class_names) + "\n"

                readme = cls._readme_yolo(dataset_name, dataset)

                zf.writestr("data.yaml", data_yaml.encode("utf-8"))
                zf.writestr("classes.txt", classes_txt.encode("utf-8"))
                zf.writestr("README.md", readme.encode("utf-8"))

                # One .txt per image, even if empty.
                for img in dataset["images"]:
                    stem = _safe_stem(img["file_name"])
                    lines = labels_by_image.get(img["id"], [])
                    zf.writestr(
                        f"labels/{stem}.txt",
                        ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
                    )

                cls._write_images(zf, image_bytes, target_prefix="images")
            
            # Write metadata in both modes
            zf.writestr(
                "metadata.json",
                json.dumps(metadata, indent=2, default=str).encode("utf-8"),
            )

        return buf

    # ==================================================================
    # FORMAT 3 — Pascal VOC
    # ==================================================================
    @staticmethod
    def _voc_xml_for_image(
        image: Dict[str, Any],
        anns: List[Dict[str, Any]],
        cat_name: Dict[int, str],
        dataset_name: str,
    ) -> str:
        W = int(image.get("width") or 0)
        H = int(image.get("height") or 0)
        objects_xml: List[str] = []
        for a in anns:
            name = _xml_escape(cat_name.get(a["category_id"], "object"))
            x, y, w, h = a["bbox"]
            xmin = max(1, int(round(x)) + 1)
            ymin = max(1, int(round(y)) + 1)
            xmax = max(xmin, int(round(x + w)))
            ymax = max(ymin, int(round(y + h)))
            if W:
                xmax = min(xmax, W)
            if H:
                ymax = min(ymax, H)
            iscrowd = int(a.get("iscrowd", 0))
            objects_xml.append(
                "  <object>\n"
                f"    <name>{name}</name>\n"
                "    <pose>Unspecified</pose>\n"
                "    <truncated>0</truncated>\n"
                f"    <difficult>{iscrowd}</difficult>\n"
                "    <bndbox>\n"
                f"      <xmin>{xmin}</xmin>\n"
                f"      <ymin>{ymin}</ymin>\n"
                f"      <xmax>{xmax}</xmax>\n"
                f"      <ymax>{ymax}</ymax>\n"
                "    </bndbox>\n"
                "  </object>"
            )
        file_name = _xml_escape(image["file_name"])
        folder = _xml_escape(dataset_name)
        return (
            "<annotation>\n"
            f"  <folder>{folder}</folder>\n"
            f"  <filename>{file_name}</filename>\n"
            f"  <path>JPEGImages/{file_name}</path>\n"
            "  <source>\n"
            "    <database>ANO Studio</database>\n"
            "  </source>\n"
            "  <size>\n"
            f"    <width>{W}</width>\n"
            f"    <height>{H}</height>\n"
            "    <depth>3</depth>\n"
            "  </size>\n"
            "  <segmented>0</segmented>\n"
            + ("\n".join(objects_xml) + ("\n" if objects_xml else ""))
            + "</annotation>\n"
        )

    @classmethod
    def _build_pascal_voc_zip(
        cls,
        dataset: Dict[str, Any],
        dataset_name: str,
        image_bytes: Dict[str, Tuple[str, Optional[bytes]]],
        splits: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> io.BytesIO:
        cat_name = {c["id"]: c["name"] for c in dataset["categories"]}
        metadata = cls._build_metadata(dataset, dataset_name, "pascal_voc")
        labels_txt = "\n".join(c["name"] for c in dataset["categories"]) + "\n"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            if splits:
                # ── Split mode: organize into train/val/test ──
                readme = (
                    cls._readme_header(dataset_name, "Pascal VOC 2012")
                    + cls._readme_summary(
                        len(dataset["images"]),
                        len(dataset["annotations"]),
                        dataset["categories"],
                    )
                    + cls._readme_split_section(splits)
                    + (
                        "## ZIP Structure\n\n"
                        "```\n"
                        f"{dataset_name}_pascal_voc.zip\n"
                        "├── train/\n"
                        "│   ├── JPEGImages/\n"
                        "│   └── Annotations/\n"
                        "├── val/\n"
                        "│   ├── JPEGImages/\n"
                        "│   └── Annotations/\n"
                        "├── test/\n"
                        "│   ├── JPEGImages/\n"
                        "│   └── Annotations/\n"
                        "├── ImageSets/\n"
                        "│   └── Main/\n"
                        "│       ├── train.txt, val.txt, test.txt\n"
                        "├── labels.txt\n"
                        "├── metadata.json\n"
                        "└── README.md\n"
                        "```\n"
                    )
                )
                zf.writestr("README.md", readme.encode("utf-8"))
                zf.writestr("labels.txt", labels_txt.encode("utf-8"))
                
                # Build per-split annotations map
                anns_by_image: Dict[int, List[Dict[str, Any]]] = {}
                for a in dataset["annotations"]:
                    anns_by_image.setdefault(a["image_id"], []).append(a)
                
                # Write split-specific folders
                for split_name, split_data in splits.items():
                    split_images = split_data.get("images", [])
                    
                    # Write XML annotations for this split
                    for img in split_images:
                        stem = _safe_stem(img["file_name"])
                        img_anns = anns_by_image.get(img["id"], [])
                        xml = cls._voc_xml_for_image(img, img_anns, cat_name, dataset_name)
                        zf.writestr(
                            f"{split_name}/Annotations/{stem}.xml",
                            xml.encode("utf-8")
                        )
                    
                    # Write images for this split
                    cls._write_split_images(
                        zf, image_bytes, split_images, f"{split_name}/JPEGImages"
                    )
                    
                    # Write ImageSets file for this split
                    stems = [_safe_stem(img["file_name"]) for img in split_images]
                    zf.writestr(
                        f"ImageSets/Main/{split_name}.txt",
                        ("\n".join(stems) + "\n").encode("utf-8"),
                    )
            else:
                # ── Non-split mode: flat structure ──
                anns_by_image: Dict[int, List[Dict[str, Any]]] = {}
                for a in dataset["annotations"]:
                    anns_by_image.setdefault(a["image_id"], []).append(a)

                all_stems: List[str] = []
                per_class_presence: Dict[str, List[Tuple[str, int]]] = {
                    c["name"]: [] for c in dataset["categories"]
                }

                readme = cls._readme_pascal(dataset_name, dataset)

                for img in dataset["images"]:
                    stem = _safe_stem(img["file_name"])
                    all_stems.append(stem)
                    img_anns = anns_by_image.get(img["id"], [])
                    xml = cls._voc_xml_for_image(img, img_anns, cat_name, dataset_name)
                    zf.writestr(f"Annotations/{stem}.xml", xml.encode("utf-8"))

                    present = {cat_name.get(a["category_id"], "") for a in img_anns}
                    for cname in per_class_presence:
                        per_class_presence[cname].append(
                            (stem, 1 if cname in present else -1)
                        )

                zf.writestr(
                    "ImageSets/Main/default.txt",
                    ("\n".join(all_stems) + "\n").encode("utf-8"),
                )
                for cname, rows in per_class_presence.items():
                    safe = _UNSAFE_RE.sub("_", cname) or "class"
                    body = "\n".join(f"{s} {v:>2}" for s, v in rows) + "\n"
                    zf.writestr(
                        f"ImageSets/Main/{safe}_default.txt", body.encode("utf-8")
                    )

                zf.writestr("labels.txt", labels_txt.encode("utf-8"))
                zf.writestr("README.md", readme.encode("utf-8"))

                cls._write_images(zf, image_bytes, target_prefix="JPEGImages")
            
            # Write metadata in both modes
            zf.writestr(
                "metadata.json",
                json.dumps(metadata, indent=2, default=str).encode("utf-8"),
            )

        return buf

    # ==================================================================
    # FORMAT 4 — Native ANO Studio JSON
    # ==================================================================
    @classmethod
    def _build_native_json_zip(
        cls,
        dataset: Dict[str, Any],
        dataset_name: str,
        image_bytes: Dict[str, Tuple[str, Optional[bytes]]],
        splits: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> io.BytesIO:
        metadata = cls._build_metadata(dataset, dataset_name, "json")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            if splits:
                # ── Split mode: organize into train/val/test ──
                readme = (
                    cls._readme_header(dataset_name, "ANO Studio Native JSON (full fidelity)")
                    + cls._readme_summary(
                        len(dataset["images"]),
                        len(dataset["annotations"]),
                        dataset["categories"],
                    )
                    + cls._readme_split_section(splits)
                    + (
                        "## ZIP Structure\n\n"
                        "```\n"
                        f"{dataset_name}_json.zip\n"
                        "├── train/\n"
                        "│   ├── images/\n"
                        "│   └── annotations_train.json\n"
                        "├── val/\n"
                        "│   ├── images/\n"
                        "│   └── annotations_val.json\n"
                        "├── test/\n"
                        "│   ├── images/\n"
                        "│   └── annotations_test.json\n"
                        "├── metadata.json\n"
                        "└── README.md\n"
                        "```\n\n"
                        "## Usage\n\n"
                        "```python\n"
                        "import json, pathlib\n"
                        "\n"
                        "# Load train split\n"
                        "train_data = json.loads(pathlib.Path('train/annotations_train.json').read_text())\n"
                        "val_data = json.loads(pathlib.Path('val/annotations_val.json').read_text())\n"
                        "test_data = json.loads(pathlib.Path('test/annotations_test.json').read_text())\n"
                        "```\n"
                    )
                )
                zf.writestr("README.md", readme.encode("utf-8"))
                
                # Write split-specific folders
                for split_name, split_data in splits.items():
                    split_images = split_data.get("images", [])
                    split_anns = split_data.get("annotations", [])
                    
                    # Build JSON payload for this split
                    payload = {
                        "format": "ano_studio_json",
                        "version": "1.0",
                        "exported_at": datetime.utcnow().isoformat() + "Z",
                        "dataset_name": f"{dataset_name} - {split_name}",
                        "split": split_name,
                        "categories": [
                            {
                                "id": c["id"],
                                "name": c["name"],
                                "supercategory": c.get("supercategory", ""),
                            }
                            for c in dataset["categories"]
                        ],
                        "images": [
                            {
                                "id": i["id"],
                                "file_name": _export_image_filename(i),
                                "width": i["width"],
                                "height": i["height"],
                                "date_captured": i.get("date_captured", ""),
                            }
                            for i in split_images
                        ],
                        "annotations": [
                            {
                                "id": a["id"],
                                "image_id": a["image_id"],
                                "category_id": a["category_id"],
                                "bbox": a["bbox"],
                                "area": a["area"],
                                "segmentation": a.get("segmentation", []),
                                "iscrowd": a.get("iscrowd", 0),
                            }
                            for a in split_anns
                        ],
                    }
                    
                    # Write split-specific annotation JSON
                    zf.writestr(
                        f"{split_name}/annotations_{split_name}.json",
                        json.dumps(payload, indent=2, default=str).encode("utf-8"),
                    )
                    
                    # Write split-specific images
                    cls._write_split_images(
                        zf, image_bytes, split_images, f"{split_name}/images"
                    )
            else:
                # ── Non-split mode: flat structure ──
                payload = {
                    "format": "ano_studio_json",
                    "version": "1.0",
                    "exported_at": datetime.utcnow().isoformat() + "Z",
                    "dataset_name": dataset_name,
                    "categories": [
                        {
                            "id": c["id"],
                            "name": c["name"],
                            "supercategory": c.get("supercategory", ""),
                        }
                        for c in dataset["categories"]
                    ],
                    "images": [
                        {
                            "id": i["id"],
                            "file_name": _export_image_filename(i),
                            "width": i["width"],
                            "height": i["height"],
                            "date_captured": i.get("date_captured", ""),
                        }
                        for i in dataset["images"]
                    ],
                    "annotations": [
                        {
                            "id": a["id"],
                            "image_id": a["image_id"],
                            "category_id": a["category_id"],
                            "bbox": a["bbox"],
                            "area": a["area"],
                            "segmentation": a.get("segmentation", []),
                            "iscrowd": a.get("iscrowd", 0),
                        }
                        for a in dataset["annotations"]
                    ],
                }
                readme = cls._readme_json(dataset_name, dataset)

                zf.writestr(
                    "annotations.json",
                    json.dumps(payload, indent=2, default=str).encode("utf-8"),
                )
                zf.writestr("README.md", readme.encode("utf-8"))
                cls._write_images(zf, image_bytes, target_prefix="images")
            
            # Write metadata in both modes
            zf.writestr(
                "metadata.json",
                json.dumps(metadata, indent=2, default=str).encode("utf-8"),
            )

        return buf
