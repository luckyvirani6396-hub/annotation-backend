"""
Pydantic schemas for COCO annotation format.
Defines data models for categories, images, annotations, and dataset storage.
Supports Roboflow-like annotation visualization and editing.
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, validator, model_validator


# ==================== COCO Base Models ====================

class Category(BaseModel):
    """COCO category model for object classes"""
    id: int = Field(..., description="Unique category ID")
    name: str = Field(..., description="Category name (e.g., 'A', 'B', '1', '2')")
    supercategory: str = Field(default="char", description="Parent category (e.g., 'char' for characters)")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": 1,
                "name": "A",
                "supercategory": "char"
            }
        }


class ImageData(BaseModel):
    """COCO image model with metadata"""
    id: int = Field(..., description="Unique image ID")
    license: int = Field(default=1, description="License ID")
    file_name: str = Field(..., description="Original filename")
    height: int = Field(..., gt=0, description="Image height in pixels")
    width: int = Field(..., gt=0, description="Image width in pixels")
    date_captured: str = Field(default="", description="Capture timestamp")
    extra: Optional[Dict[str, Any]] = Field(default=None, description="Additional metadata like plate_text")
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": 1,
                "license": 1,
                "file_name": "UP80PH5881_20260401_001828_654440_q94_crop.jpg",
                "height": 66,
                "width": 122,
                "date_captured": "2026-04-01T00:18:28.654000",
                "extra": {
                    "name": "UP80PH5881_20260401_001828_654440_q94_crop.jpg",
                    "plate_text": "UP80PH581"
                }
            }
        }


class Annotation(BaseModel):
    """COCO annotation model with bounding box coordinates"""
    id: int = Field(..., description="Unique annotation ID")
    image_id: int = Field(..., description="ID of the image this annotation belongs to")
    category_id: int = Field(..., description="ID of the category/class")
    bbox: List[float] = Field(
        ..., 
        min_items=4, 
        max_items=4,
        description="Bounding box [x, y, width, height]"
    )
    area: float = Field(..., description="Area of the bounding box (width * height)")
    segmentation: List[List[float]] = Field(
        default_factory=list,
        description="Polygon segmentation points (optional)"
    )
    iscrowd: int = Field(default=0, description="0 = single object, 1 = crowd")
    
    @validator('bbox')
    def validate_bbox(cls, v):
        """Validate bounding box coordinates"""
        if len(v) != 4:
            raise ValueError('bbox must have exactly 4 values [x, y, width, height]')
        
        x, y, w, h = v
        
        if w <= 0:
            raise ValueError(f'Width must be positive, got {w}')
        
        if h <= 0:
            raise ValueError(f'Height must be positive, got {h}')
        
        if x < 0:
            raise ValueError(f'X coordinate cannot be negative, got {x}')
        
        if y < 0:
            raise ValueError(f'Y coordinate cannot be negative, got {y}')
        
        return v
    
    @validator('area')
    def validate_area(cls, v, values):
        """Validate or auto-correct area based on bbox dimensions"""
        if 'bbox' in values:
            bbox = values['bbox']
            if len(bbox) == 4:
                expected_area = bbox[2] * bbox[3]
                # Allow small floating point differences
                if abs(v - expected_area) > 0.01:
                    # Auto-correct area
                    return expected_area
        return v
    
    @validator('segmentation')
    def validate_segmentation(cls, v):
        """Validate segmentation format"""
        if v:  # If segmentation is provided
            for segment in v:
                if len(segment) % 2 != 0:
                    raise ValueError('Segmentation points must be in pairs (x,y)')
        return v
    
    class Config:
        json_schema_extra = {
            "example": {
                "id": 1,
                "image_id": 1,
                "category_id": 1,
                "bbox": [100, 150, 50, 60],
                "area": 3000,
                "segmentation": [],
                "iscrowd": 0
            }
        }


class COCOJSON(BaseModel):
    """Complete COCO format JSON structure for dataset upload"""
    info: Optional[Dict[str, Any]] = Field(default=None, description="Dataset information")
    licenses: Optional[List[Dict[str, Any]]] = Field(default=None, description="License information")
    categories: List[Category] = Field(..., description="List of categories/classes")
    images: List[ImageData] = Field(..., description="List of images")
    annotations: List[Annotation] = Field(..., description="List of annotations")
    
    # Use model_validator instead of root_validator (Pydantic V2 syntax)
    @model_validator(mode='after')
    def validate_unique_ids(self) -> 'COCOJSON':
        """Ensure all IDs are unique across the dataset"""
        # Check unique category IDs
        cat_ids = [cat.id for cat in self.categories]
        if len(cat_ids) != len(set(cat_ids)):
            raise ValueError('Category IDs must be unique')
        
        # Check unique image IDs
        img_ids = [img.id for img in self.images]
        if len(img_ids) != len(set(img_ids)):
            raise ValueError('Image IDs must be unique')
        
        # Check unique annotation IDs
        ann_ids = [ann.id for ann in self.annotations]
        if len(ann_ids) != len(set(ann_ids)):
            raise ValueError('Annotation IDs must be unique')
        
        # Validate annotation references
        img_id_set = set(img_ids)
        cat_id_set = set(cat_ids)
        
        for ann in self.annotations:
            if ann.image_id not in img_id_set:
                raise ValueError(f'Annotation {ann.id} references non-existent image {ann.image_id}')
            if ann.category_id not in cat_id_set:
                raise ValueError(f'Annotation {ann.id} references non-existent category {ann.category_id}')
        
        return self
    
    class Config:
        json_schema_extra = {
            "example": {
                "info": {
                    "description": "License Plate Dataset",
                    "version": "1.0",
                    "year": 2026
                },
                "licenses": [{"id": 1, "name": "MIT License"}],
                "categories": [
                    {"id": 1, "name": "A", "supercategory": "char"},
                    {"id": 2, "name": "B", "supercategory": "char"}
                ],
                "images": [
                    {
                        "id": 1,
                        "license": 1,
                        "file_name": "image1.jpg",
                        "height": 480,
                        "width": 640,
                        "date_captured": "2026-01-01T00:00:00",
                        "extra": {"plate_text": "ABC123"}
                    }
                ],
                "annotations": [
                    {
                        "id": 1,
                        "image_id": 1,
                        "category_id": 1,
                        "bbox": [100, 150, 50, 60],
                        "area": 3000,
                        "segmentation": [],
                        "iscrowd": 0
                    }
                ]
            }
        }


# ==================== Database Document Models ====================

class DatasetMetadata(BaseModel):
    """MongoDB document for dataset-level metadata"""
    dataset_name: str = Field(..., description="Unique dataset name")
    total_categories: int = Field(default=0, description="Number of categories")
    total_images: int = Field(default=0, description="Number of images")
    total_annotations: int = Field(default=0, description="Number of annotations")
    uploaded_at: datetime = Field(default_factory=datetime.utcnow, description="Upload timestamp")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="Last update timestamp")
    is_active: bool = Field(default=True, description="Soft delete flag")
    description: Optional[str] = Field(default=None, description="Dataset description")
    tags: List[str] = Field(default_factory=list, description="Dataset tags")
    
    class Config:
        json_schema_extra = {
            "example": {
                "dataset_name": "license_plates_v1",
                "total_categories": 36,
                "total_images": 1000,
                "total_annotations": 5000,
                "is_active": True,
                "tags": ["license_plates", "ocr"]
            }
        }


class CategoryDocument(BaseModel):
    """MongoDB document for individual category storage"""
    dataset_id: str = Field(..., description="Reference to dataset")
    dataset_name: str = Field(..., description="Dataset name for easy querying")
    category_id: int = Field(..., description="Original category ID from COCO")
    name: str = Field(..., description="Category name")
    supercategory: str = Field(default="char", description="Parent category")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Creation timestamp")
    
    class Config:
        json_schema_extra = {
            "example": {
                "dataset_id": "507f1f77bcf86cd799439011",
                "dataset_name": "license_plates_v1",
                "category_id": 1,
                "name": "A",
                "supercategory": "char"
            }
        }


class ImageDocument(BaseModel):
    """MongoDB document for individual image storage"""
    dataset_id: str = Field(..., description="Reference to dataset")
    dataset_name: str = Field(..., description="Dataset name for easy querying")
    image_id: int = Field(..., description="Original image ID from COCO")
    file_name: str = Field(..., description="Image filename")
    height: int = Field(..., description="Image height in pixels")
    width: int = Field(..., description="Image width in pixels")
    date_captured: str = Field(default="", description="Capture timestamp")
    extra: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    uploaded_at: datetime = Field(default_factory=datetime.utcnow, description="Upload timestamp")
    
    class Config:
        json_schema_extra = {
            "example": {
                "dataset_id": "507f1f77bcf86cd799439011",
                "dataset_name": "license_plates_v1",
                "image_id": 1,
                "file_name": "UP80PH5881_crop.jpg",
                "height": 66,
                "width": 122,
                "date_captured": "2026-04-01T00:18:28.654000",
                "extra": {"plate_text": "UP80PH581"}
            }
        }


class AnnotationDocument(BaseModel):
    """MongoDB document for individual annotation storage (bounding boxes)"""
    dataset_id: str = Field(..., description="Reference to dataset")
    dataset_name: str = Field(..., description="Dataset name for easy querying")
    annotation_id: int = Field(..., description="Original annotation ID from COCO")
    image_id: int = Field(..., description="Reference to image ID")
    category_id: int = Field(..., description="Reference to category ID")
    bbox: List[float] = Field(..., description="Bounding box [x, y, width, height]")
    area: float = Field(..., description="Area of bounding box")
    segmentation: List[List[float]] = Field(default_factory=list, description="Segmentation polygon")
    iscrowd: int = Field(default=0, description="Crowd flag")
    uploaded_at: datetime = Field(default_factory=datetime.utcnow, description="Upload timestamp")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="Last update timestamp")
    
    class Config:
        json_schema_extra = {
            "example": {
                "dataset_id": "507f1f77bcf86cd799439011",
                "dataset_name": "license_plates_v1",
                "annotation_id": 1,
                "image_id": 1,
                "category_id": 1,
                "bbox": [100, 150, 50, 60],
                "area": 3000,
                "segmentation": [],
                "iscrowd": 0
            }
        }


# ==================== Request/Response Models ====================

class CreateDatasetRequest(BaseModel):
    """Request model for creating a new dataset"""
    dataset_name: str = Field(
        ..., 
        min_length=1, 
        max_length=100,
        pattern=r'^[a-zA-Z0-9_-]+$',
        description="Unique dataset name (alphanumeric, underscore, hyphen only)"
    )
    description: Optional[str] = Field(default=None, max_length=500, description="Dataset description")
    tags: List[str] = Field(default_factory=list, description="Searchable tags for the dataset")
    
    class Config:
        json_schema_extra = {
            "example": {
                "dataset_name": "license_plates_v1",
                "description": "License plate character annotations for OCR",
                "tags": ["license_plates", "ocr", "characters"]
            }
        }


class UpdateAnnotationRequest(BaseModel):
    """Request model for batch annotation updates"""
    annotations: List[Annotation] = Field(..., description="List of annotations to update")
    update_mode: str = Field(
        default="upsert",
        description="Update mode: 'upsert' (update or insert), 'replace' (replace all), 'delete' (remove specified)"
    )
    
    @validator('update_mode')
    def validate_mode(cls, v):
        allowed = ['upsert', 'replace', 'delete']
        if v not in allowed:
            raise ValueError(f'update_mode must be one of {allowed}')
        return v


class DatasetInfoResponse(BaseModel):
    """Response model for dataset information"""
    dataset_name: str
    dataset_id: str
    total_annotations: int
    total_images: int
    total_categories: int
    uploaded_at: str
    updated_at: str
    category_distribution: List[Dict[str, Any]]
    avg_annotations_per_image: float
    is_active: bool
    
    class Config:
        json_schema_extra = {
            "example": {
                "dataset_name": "license_plates_v1",
                "dataset_id": "507f1f77bcf86cd799439011",
                "total_annotations": 5000,
                "total_images": 1000,
                "total_categories": 36,
                "avg_annotations_per_image": 5.0,
                "is_active": True
            }
        }


class ImageWithAnnotationsResponse(BaseModel):
    """Response model for image with all its annotations (Roboflow-like format)"""
    image: Dict[str, Any] = Field(..., description="Image metadata")
    annotations: List[Dict[str, Any]] = Field(..., description="List of annotations with category names")
    total_annotations: int = Field(..., description="Total number of annotations")
    dataset_name: str = Field(..., description="Dataset name")
    
    class Config:
        json_schema_extra = {
            "example": {
                "image": {
                    "id": 1,
                    "file_name": "image1.jpg",
                    "width": 640,
                    "height": 480
                },
                "annotations": [
                    {
                        "id": 1,
                        "bbox": [100, 150, 50, 60],
                        "category_id": 1,
                        "category_name": "A",
                        "area": 3000
                    }
                ],
                "total_annotations": 1,
                "dataset_name": "license_plates_v1"
            }
        }


class CategoryWithStats(BaseModel):
    """Category with annotation statistics"""
    category_id: int
    name: str
    supercategory: str
    annotation_count: int
    image_count: int
    
    class Config:
        json_schema_extra = {
            "example": {
                "category_id": 1,
                "name": "A",
                "supercategory": "char",
                "annotation_count": 150,
                "image_count": 120
            }
        }


# ==================== Helper Functions ====================

def coco_to_dict(coco_data: COCOJSON) -> Dict[str, Any]:
    """
    Convert COCOJSON model to dictionary for database storage
    
    Args:
        coco_data: COCOJSON model instance
    
    Returns:
        Dictionary representation suitable for MongoDB storage
    """
    return {
        "categories": [cat.dict() for cat in coco_data.categories],
        "images": [img.dict() for img in coco_data.images],
        "annotations": [ann.dict() for ann in coco_data.annotations]
    }


def dict_to_coco(data: Dict[str, Any]) -> COCOJSON:
    """
    Convert dictionary to COCOJSON model
    
    Args:
        data: Dictionary with categories, images, annotations keys
    
    Returns:
        COCOJSON model instance
    """
    return COCOJSON(
        info=data.get("info"),
        licenses=data.get("licenses"),
        categories=[Category(**cat) for cat in data.get("categories", [])],
        images=[ImageData(**img) for img in data.get("images", [])],
        annotations=[Annotation(**ann) for ann in data.get("annotations", [])]
    )


def calculate_iou(box1: List[float], box2: List[float]) -> float:
    """
    Calculate Intersection over Union (IoU) between two bounding boxes
    
    Args:
        box1: First bounding box [x, y, width, height]
        box2: Second bounding box [x, y, width, height]
    
    Returns:
        IoU score between 0 and 1
    """
    # Convert to [x1, y1, x2, y2] format
    x1_1, y1_1 = box1[0], box1[1]
    x2_1, y2_1 = box1[0] + box1[2], box1[1] + box1[3]
    
    x1_2, y1_2 = box2[0], box2[1]
    x2_2, y2_2 = box2[0] + box2[2], box2[1] + box2[3]
    
    # Calculate intersection
    xi1 = max(x1_1, x1_2)
    yi1 = max(y1_1, y1_2)
    xi2 = min(x2_1, x2_2)
    yi2 = min(y2_1, y2_2)
    
    if xi2 <= xi1 or yi2 <= yi1:
        return 0.0
    
    intersection = (xi2 - xi1) * (yi2 - yi1)
    
    # Calculate union
    area1 = box1[2] * box1[3]
    area2 = box2[2] * box2[3]
    union = area1 + area2 - intersection
    
    return intersection / union if union > 0 else 0.0


def normalize_bbox(bbox: List[float], image_width: int, image_height: int) -> List[float]:
    """
    Normalize bounding box coordinates to 0-1 range
    
    Args:
        bbox: [x, y, width, height] in absolute pixels
        image_width: Width of the image in pixels
        image_height: Height of the image in pixels
    
    Returns:
        Normalized bounding box [x_norm, y_norm, width_norm, height_norm]
    """
    return [
        bbox[0] / image_width,
        bbox[1] / image_height,
        bbox[2] / image_width,
        bbox[3] / image_height
    ]


def denormalize_bbox(bbox_norm: List[float], image_width: int, image_height: int) -> List[float]:
    """
    Convert normalized bounding box to absolute pixel coordinates
    
    Args:
        bbox_norm: [x, y, width, height] in 0-1 range
        image_width: Width of the image in pixels
        image_height: Height of the image in pixels
    
    Returns:
        Absolute bounding box [x, y, width, height] in pixels
    """
    return [
        bbox_norm[0] * image_width,
        bbox_norm[1] * image_height,
        bbox_norm[2] * image_width,
        bbox_norm[3] * image_height
    ]