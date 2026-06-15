from typing import List, Dict, Any
from loguru import logger

def validate_images_exist(images_in_json: List[Any], uploaded_filenames: List[str]) -> List[str]:
    """Validate that images referenced in JSON exist in uploaded files"""
    json_filenames = [img.file_name for img in images_in_json]
    missing = [f for f in json_filenames if f not in uploaded_filenames]
    
    if missing:
        logger.warning(f"Missing images: {missing}")
    
    return missing

def validate_annotation_format(annotation: Dict[str, Any]) -> bool:
    """Validate annotation format"""
    required_fields = ['id', 'image_id', 'category_id', 'bbox', 'area']
    
    for field in required_fields:
        if field not in annotation:
            logger.error(f"Missing required field in annotation: {field}")
            return False
    
    # Validate bbox format
    bbox = annotation['bbox']
    if len(bbox) != 4:
        logger.error(f"Invalid bbox format: {bbox}")
        return False
    
    return True

def validate_file_extension(filename: str, allowed_extensions: List[str]) -> bool:
    """Validate file extension"""
    import os
    ext = os.path.splitext(filename)[1].lower()
    return ext in allowed_extensions