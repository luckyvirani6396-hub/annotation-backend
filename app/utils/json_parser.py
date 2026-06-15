import json
from typing import Dict, Any, List
from pathlib import Path
from loguru import logger

class JSONParser:
    @staticmethod
    def parse_coco_json(json_data: str) -> Dict[str, Any]:
        """Parse COCO format JSON string"""
        try:
            data = json.loads(json_data)
            
            # Validate COCO format
            required_keys = ['categories', 'images', 'annotations']
            for key in required_keys:
                if key not in data:
                    raise ValueError(f"Missing required key: {key}")
            
            return data
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"COCO validation error: {str(e)}")
            raise

    @staticmethod
    def parse_from_file(file_path: Path) -> Dict[str, Any]:
        """Parse JSON file"""
        try:
            with open(file_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to parse file {file_path}: {str(e)}")
            raise