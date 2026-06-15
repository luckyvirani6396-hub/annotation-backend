"""Pydantic schemas: request/response models and persisted documents."""

from __future__ import annotations

from app.schemas.annotation import (
    Annotation,
    AnnotationDocument,
    Category,
    CategoryDocument,
    CategoryWithStats,
    COCOJSON,
    CreateDatasetRequest,
    DatasetInfoResponse,
    DatasetMetadata,
    ImageData,
    ImageDocument,
    ImageWithAnnotationsResponse,
    UpdateAnnotationRequest,
    calculate_iou,
    coco_to_dict,
    denormalize_bbox,
    dict_to_coco,
    normalize_bbox,
)
from app.schemas.response import (
    AnnotationResponse,
    ExportResponse,
    PaginatedResponse,
    ResponseModel,
    UploadResponse,
)

__all__ = [
    # annotation models
    "Annotation",
    "AnnotationDocument",
    "Category",
    "CategoryDocument",
    "CategoryWithStats",
    "COCOJSON",
    "CreateDatasetRequest",
    "DatasetInfoResponse",
    "DatasetMetadata",
    "ImageData",
    "ImageDocument",
    "ImageWithAnnotationsResponse",
    "UpdateAnnotationRequest",
    # annotation helpers
    "calculate_iou",
    "coco_to_dict",
    "denormalize_bbox",
    "dict_to_coco",
    "normalize_bbox",
    # response envelopes
    "AnnotationResponse",
    "ExportResponse",
    "PaginatedResponse",
    "ResponseModel",
    "UploadResponse",
]
