from typing import Optional, List, Any, Generic, TypeVar
from pydantic import BaseModel
from datetime import datetime

T = TypeVar('T')

class ResponseModel(BaseModel, Generic[T]):
    success: bool
    message: str
    data: Optional[T] = None
    error: Optional[str] = None

class PaginatedResponse(BaseModel, Generic[T]):
    items: List[T]
    total: int
    skip: int
    limit: int
    
    @property
    def has_next(self) -> bool:
        return self.skip + self.limit < self.total
    
    @property
    def has_previous(self) -> bool:
        return self.skip > 0

class UploadResponse(BaseModel):
    upload_id: str
    images_uploaded: List[str]
    images_failed: List[str]
    total_images: int
    annotation_processed: bool

class AnnotationResponse(BaseModel):
    dataset_name: str
    total_categories: int
    total_images: int
    total_annotations: int
    uploaded_at: datetime

class ExportResponse(BaseModel):
    download_url: str
    file_name: str
    file_size: int