"""
Annotations API endpoints for managing COCO format annotation datasets.
Provides CRUD operations, pagination, filtering, and export capabilities.
"""

from typing import Optional, List
# pyrefly: ignore [missing-import]
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Depends
# pyrefly: ignore [missing-import]
from fastapi.responses import JSONResponse
# pyrefly: ignore [missing-import]
from loguru import logger
from datetime import datetime
from pydantic import BaseModel, Field

# pyrefly: ignore [missing-import]
from bson import ObjectId
from app.auth.dependencies import get_current_active_user
from app.services.annotation_service import AnnotationService
from app.schemas.response import ResponseModel, PaginatedResponse
from app.schemas.annotation import Annotation, Category, ImageData
from app.config.database import get_collection


async def _resolve_owner_user_id(
    dataset_name: str,
    batch_id: Optional[str],
    current_user_id: str,
) -> str:
    """
    For batch-mode access, look up the dataset owner's user_id so cross-user
    reads/writes work correctly. The owner's user_id is the namespace under which
    images and annotations live in MongoDB.
    """
    if not batch_id:
        return current_user_id
    owner_id = await AnnotationService.get_dataset_owner_id(dataset_name)
    return owner_id if owner_id else current_user_id


router = APIRouter()


# Request bodies for Roboflow-style category management
class CategoryCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    supercategory: Optional[str] = "object"


class CategoryUpdateBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    supercategory: Optional[str] = None

# ==================== GET Endpoints ====================

@router.get("/datasets", response_model=ResponseModel)
async def get_datasets(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    search: Optional[str] = Query(None, description="Search by dataset name"),
    sort_by: str = Query("uploaded_at", description="Sort field"),
    sort_desc: bool = Query(True, description="Sort descending"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get all annotation datasets for the authenticated user with pagination and filtering.
    
    - **skip**: Number of records to skip (pagination)
    - **limit**: Maximum number of records to return
    - **search**: Filter datasets by name (case-insensitive)
    - **sort_by**: Sort by field (uploaded_at, dataset_name, total_images)
    - **sort_desc**: Sort descending order
    """
    try:
        user_id = current_user["_id"]
        datasets = await AnnotationService.get_all_datasets(
            user_id=user_id,
            skip=skip, 
            limit=limit, 
            search=search,
            sort_by=sort_by,
            sort_desc=sort_desc
        )
        
        total_count = await AnnotationService.get_datasets_count(user_id=user_id, search=search)
        
        return ResponseModel(
            success=True,
            message="Datasets retrieved successfully",
            data=PaginatedResponse(
                items=[
                    {
                        "dataset_name": ds["dataset_name"],
                        "total_images": ds["total_images"],
                        "total_annotations": ds["total_annotations"],
                        "total_categories": ds["total_categories"],
                        "uploaded_at": ds["uploaded_at"],
                        "updated_at": ds["updated_at"]
                    }
                    for ds in datasets
                ],
                total=total_count,
                skip=skip,
                limit=limit
            )
        )
        
    except Exception as e:
        logger.error(f"Failed to get datasets: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve datasets: {str(e)}")

@router.get("/{dataset_name}/info", response_model=ResponseModel)
async def get_dataset_info(
    dataset_name: str,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get comprehensive information about a specific dataset including statistics.
    """
    try:
        user_role = current_user.get("role", "annotator")
        if user_role == "admin":
            owner_id = await AnnotationService.get_dataset_owner_id(dataset_name)
            user_id = owner_id if owner_id else current_user["_id"]
        else:
            user_id = current_user["_id"]
            
        dataset_info = await AnnotationService.get_dataset_info(dataset_name, user_id)
        
        if not dataset_info:
            raise HTTPException(status_code=404, detail=f"Dataset '{dataset_name}' not found")
        
        return ResponseModel(
            success=True,
            message="Dataset info retrieved successfully",
            data=dataset_info
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get dataset info: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{dataset_name}", response_model=ResponseModel)
async def get_annotations(
    dataset_name: str,
    image_id: Optional[int] = Query(None, description="Filter by image ID"),
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    limit: int = Query(500, ge=1, le=10000, description="Maximum annotations to return"),
    skip: int = Query(0, ge=0, description="Number of annotations to skip"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get annotations for a specific dataset with filtering options.
    
    - **dataset_name**: Name of the dataset
    - **image_id**: Filter annotations by specific image
    - **category_id**: Filter annotations by specific category
    - **limit**: Maximum number of annotations to return
    - **skip**: Number of annotations to skip (pagination)
    """
    try:
        user_role = current_user.get("role", "annotator")
        if user_role == "admin":
            owner_id = await AnnotationService.get_dataset_owner_id(dataset_name)
            user_id = owner_id if owner_id else current_user["_id"]
        else:
            user_id = current_user["_id"]
            
        # Get dataset metadata
        dataset = await AnnotationService.get_annotation_dataset(dataset_name, user_id)
        
        if not dataset:
            raise HTTPException(status_code=404, detail=f"Dataset '{dataset_name}' not found")
        
        # Get annotations with filters
        annotations = await AnnotationService.get_annotations(
            dataset_name=dataset_name,
            user_id=user_id,
            image_id=image_id,
            category_id=category_id,
            limit=limit,
            skip=skip
        )
        
        total_count = await AnnotationService.get_annotations_count(
            dataset_name=dataset_name,
            user_id=user_id,
            image_id=image_id,
            category_id=category_id
        )
        
        return ResponseModel(
            success=True,
            message="Annotations retrieved successfully",
            data={
                "dataset_name": dataset_name,
                "total_count": total_count,
                "returned_count": len(annotations),
                "annotations": annotations,
                "filters_applied": {
                    "image_id": image_id,
                    "category_id": category_id
                }
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get annotations: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{dataset_name}/categories", response_model=ResponseModel)
async def get_dataset_categories(
    dataset_name: str,
    batch_id: Optional[str] = Query(None, description="Batch context for cross-user reads"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get all categories for a specific dataset with statistics.
    When `batch_id` is supplied, categories are read from the dataset owner's
    namespace so an assigned annotator sees the same class list as the admin.
    """
    try:
        user_id = await _resolve_owner_user_id(dataset_name, batch_id, str(current_user["_id"]))
        categories = await AnnotationService.get_categories_with_stats(dataset_name, user_id)

        # Note: an empty list is a valid state right after an image-only
        # upload — the user hasn't created any classes yet.  Return [] instead
        # of 404 so the annotation workspace can render the empty Classes UI.
        return ResponseModel(
            success=True,
            message="Categories retrieved successfully",
            data={
                "dataset_name": dataset_name,
                "total_categories": len(categories),
                "categories": categories
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get categories: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{dataset_name}/categories", response_model=ResponseModel)
async def create_dataset_category(
    dataset_name: str,
    body: CategoryCreateBody,
    current_user: dict = Depends(get_current_active_user),
):
    """Create a new class/category inside a dataset (Roboflow-style)."""
    try:
        user_id = current_user["_id"]
        category = await AnnotationService.create_category(
            dataset_name=dataset_name,
            user_id=user_id,
            name=body.name,
            supercategory=body.supercategory or "object",
        )
        return ResponseModel(
            success=True,
            message=f"Category '{category['name']}' created",
            data={"category": category},
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Failed to create category: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{dataset_name}/categories/{category_id}", response_model=ResponseModel)
async def update_dataset_category(
    dataset_name: str,
    category_id: int,
    body: CategoryUpdateBody,
    current_user: dict = Depends(get_current_active_user),
):
    """Rename or change the supercategory of an existing class."""
    try:
        user_id = current_user["_id"]
        result = await AnnotationService.update_category(
            dataset_name=dataset_name,
            user_id=user_id,
            category_id=category_id,
            name=body.name,
            supercategory=body.supercategory,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Category not found")
        return ResponseModel(success=True, message="Category updated", data={"category": result})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update category: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{dataset_name}/categories/{category_id}", response_model=ResponseModel)
async def delete_dataset_category(
    dataset_name: str,
    category_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    """Delete a class and cascade-remove all annotations referencing it."""
    try:
        user_id = current_user["_id"]
        ok = await AnnotationService.delete_category(
            dataset_name=dataset_name,
            user_id=user_id,
            category_id=category_id,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Category not found")
        return ResponseModel(success=True, message="Category deleted", data={"id": category_id})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete category: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{dataset_name}/images", response_model=ResponseModel)
async def get_images_paginated(
    dataset_name: str,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Images per page"),
    search: Optional[str] = Query(None, description="Search by filename"),
    annotation_status: Optional[str] = Query(None, description="Filter: 'annotated', 'not_annotated', or None for all"),
    include_svg: bool = Query(False, description="Include SVG visualization"),
    batch_id: Optional[str] = Query(None, description="Restrict to images in this batch"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get paginated images with page-based pagination for the UI gallery.
    
    - **page**: Page number starting from 1
    - **page_size**: Number of images per page (default 20)
    - **search**: Filter images by filename (case-insensitive)
    - **annotation_status**: 'annotated' | 'not_annotated' | None
    - **batch_id**: When provided, restrict to images in that batch (cross-user access)
    - Returns pagination metadata: total_images, total_pages, current_page, has_next, has_previous
    """
    try:
        user_id = await _resolve_owner_user_id(dataset_name, batch_id, str(current_user["_id"]))
        logger.info(f"\U0001f50d Fetching images for {dataset_name}: page={page}, page_size={page_size}, search={search}, batch_id={batch_id}")

        # When restricting to a batch, load the image_id range
        image_id_min: Optional[int] = None
        image_id_max: Optional[int] = None
        if batch_id and ObjectId.is_valid(batch_id):
            batch_doc = await get_collection("task_batches").find_one({"_id": ObjectId(batch_id)})
            if batch_doc:
                image_id_min = batch_doc.get("start_index")
                image_id_max = batch_doc.get("end_index")

        result = await AnnotationService.get_paginated_images(
            dataset_name=dataset_name,
            user_id=user_id,
            page=page,
            page_size=page_size,
            search=search,
            annotation_status=annotation_status,
            include_svg=include_svg,
            image_id_min=image_id_min,
            image_id_max=image_id_max,
        )
        
        if result is None:
            raise HTTPException(status_code=404, detail=f"Dataset '{dataset_name}' not found")
        
        # Log pagination info
        pagination = result.get("pagination", {})
        logger.info(f"✅ Returned {len(result.get('images', []))} images | Total: {pagination.get('total_images')} | Pages: {pagination.get('total_pages')}")
        
        return ResponseModel(
            success=True,
            message="Images retrieved successfully",
            data=result
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get paginated images: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{dataset_name}/images/{image_id}", response_model=ResponseModel)
async def get_image_with_annotations(
    dataset_name: str,
    image_id: int,
    include_svg: bool = Query(False, description="Include SVG visualization"),
    batch_id: Optional[str] = Query(None, description="Batch context for cross-user access"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get a specific image with its annotations.
    
    - **include_svg**: Generate SVG visualization of bounding boxes
    - **batch_id**: When provided, allows cross-user access (annotator viewing admin's dataset)
    """
    try:
        user_id = await _resolve_owner_user_id(dataset_name, batch_id, str(current_user["_id"]))
        result = await AnnotationService.get_image_with_annotations(
            dataset_name=dataset_name,
            image_id=image_id,
            user_id=user_id,
            include_svg=include_svg
        )
        
        if not result:
            raise HTTPException(status_code=404, detail=f"Image {image_id} not found in dataset {dataset_name}")
        
        return ResponseModel(
            success=True,
            message="Image with annotations retrieved successfully",
            data=result
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get image with annotations: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{dataset_name}/stats/summary", response_model=ResponseModel)
async def get_dataset_statistics(
    dataset_name: str,
    batch_id: Optional[str] = Query(None, description="Batch context for cross-user reads"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Get comprehensive statistics about the dataset.
    """
    try:
        user_id = await _resolve_owner_user_id(dataset_name, batch_id, str(current_user["_id"]))
        stats = await AnnotationService.get_dataset_statistics(dataset_name, user_id)
        
        if not stats:
            raise HTTPException(status_code=404, detail=f"Dataset '{dataset_name}' not found")
        
        return ResponseModel(
            success=True,
            message="Statistics retrieved successfully",
            data=stats
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get statistics: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== POST/PUT Endpoints ====================

@router.put("/{dataset_name}/annotations", response_model=ResponseModel)
async def update_annotations_batch(
    dataset_name: str,
    annotations: List[Annotation],
    background_tasks: BackgroundTasks,
    batch_id: Optional[str] = Query(None, description="Batch context for cross-user annotation saves"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Update multiple annotations for a dataset in batch.
    
    - **background_tasks**: Process updates in background for large batches
    - **batch_id**: When provided, uses dataset owner's user_id so annotations are saved correctly
    """
    try:
        user_id = await _resolve_owner_user_id(dataset_name, batch_id, str(current_user["_id"]))
        # Validate dataset exists
        dataset = await AnnotationService.get_annotation_dataset(dataset_name, user_id)
        if not dataset:
            raise HTTPException(status_code=404, detail=f"Dataset '{dataset_name}' not found")
        
        # Process updates
        if len(annotations) > 1000:
            # Handle large batches in background
            background_tasks.add_task(
                AnnotationService.batch_update_annotations,
                dataset_name=dataset_name,
                user_id=user_id,
                annotations=annotations
            )
            return ResponseModel(
                success=True,
                message=f"Batch update initiated for {len(annotations)} annotations. Processing in background.",
                data={"status": "processing", "total": len(annotations)}
            )
        else:
            # Process small batches synchronously
            updated_count = await AnnotationService.batch_update_annotations(
                dataset_name=dataset_name,
                user_id=user_id,
                annotations=annotations
            )
            
            return ResponseModel(
                success=True,
                message=f"Successfully updated {updated_count} annotations",
                data={
                    "dataset_name": dataset_name,
                    "updated_count": updated_count,
                    "total_requested": len(annotations)
                }
            )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update annotations batch: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{dataset_name}/annotations", response_model=ResponseModel)
async def add_annotation(
    dataset_name: str,
    annotation: Annotation,
    batch_id: Optional[str] = Query(None, description="Batch context for cross-user saves"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Add a single annotation to the dataset.
    When batch_id is provided, the annotation is saved under the dataset owner's user_id
    and the current user's id is stored as annotator_id for attribution.
    """
    try:
        user_id = await _resolve_owner_user_id(dataset_name, batch_id, str(current_user["_id"]))
        annotator_id = str(current_user["_id"]) if batch_id else None
        result = await AnnotationService.add_annotation(
            dataset_name, user_id, annotation,
            annotator_id=annotator_id, batch_id=batch_id
        )
        
        if not result:
            raise HTTPException(status_code=404, detail=f"Dataset '{dataset_name}' not found")
        
        return ResponseModel(
            success=True,
            message="Annotation added successfully",
            data={
                "annotation_id": annotation.id,
                "dataset_name": dataset_name,
                "image_id": annotation.image_id
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add annotation: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== DELETE Endpoints ====================

@router.delete("/{dataset_name}", response_model=ResponseModel)
async def delete_dataset(
    dataset_name: str,
    hard_delete: bool = Query(False, description="Permanently delete all data"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Delete a dataset and all its associated data.
    
    - **hard_delete**: If False, soft delete (mark as deleted). If True, permanently remove.
    """
    try:
        user_role = current_user.get("role", "annotator")
        if user_role == "admin":
            owner_id = await AnnotationService.get_dataset_owner_id(dataset_name)
            user_id = owner_id if owner_id else current_user["_id"]
        else:
            user_id = current_user["_id"]
            
        deleted = await AnnotationService.delete_dataset(
            dataset_name=dataset_name,
            user_id=user_id,
            hard_delete=hard_delete
        )
        
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Dataset '{dataset_name}' not found")
        
        return ResponseModel(
            success=True,
            message=f"Dataset '{dataset_name}' {'permanently ' if hard_delete else ''}deleted successfully",
            data={
                "dataset_name": dataset_name,
                "deleted_at": datetime.utcnow().isoformat(),
                "hard_delete": hard_delete
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete dataset: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{dataset_name}/annotations/{annotation_id}", response_model=ResponseModel)
async def delete_annotation(
    dataset_name: str,
    annotation_id: int,
    batch_id: Optional[str] = Query(None, description="Batch context for cross-user deletes"),
    current_user: dict = Depends(get_current_active_user),
):
    """
    Delete a specific annotation from the dataset.
    When batch_id is provided, the annotation is removed from the dataset
    owner's namespace so checkers/annotators can correct batch work.
    """
    try:
        user_id = await _resolve_owner_user_id(dataset_name, batch_id, str(current_user["_id"]))
        deleted = await AnnotationService.delete_annotation(dataset_name, user_id, annotation_id)
        
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Annotation {annotation_id} not found")
        
        return ResponseModel(
            success=True,
            message=f"Annotation {annotation_id} deleted successfully",
            data={
                "dataset_name": dataset_name,
                "annotation_id": annotation_id
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete annotation: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# @router.delete("/{dataset_name}/images/{image_id}/annotations", response_model=ResponseModel)
# async def delete_image_annotations(
#     dataset_name: str,
#     image_id: int,
#     current_user: dict = Depends(get_current_active_user),
# ):
#     """
#     Delete all annotations for a specific image.
#     """
#     try:
#         user_id = current_user["_id"]
#         deleted_count = await AnnotationService.delete_image_annotations(dataset_name, user_id, image_id)
        
#         return ResponseModel(
#             success=True,
#             message=f"Deleted {deleted_count} annotations for image {image_id}",
#             data={
#                 "dataset_name": dataset_name,
#                 "image_id": image_id,
#                 "deleted_count": deleted_count
#             }
#         )
        
#     except Exception as e:
#         logger.error(f"Failed to delete image annotations: {str(e)}")
#         raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{dataset_name}/images/{image_id}", response_model=ResponseModel)
async def delete_image(
    dataset_name: str,
    image_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Delete an image and all its associated annotations from the dataset.
    """
    try:
        user_id = current_user["_id"]
        success = await AnnotationService.delete_image(dataset_name, user_id, image_id)
        
        if not success:
            raise HTTPException(status_code=404, detail=f"Image {image_id} not found")
        
        return ResponseModel(
            success=True,
            message=f"Image {image_id} deleted successfully",
            data={
                "dataset_name": dataset_name,
                "image_id": image_id
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete image: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))