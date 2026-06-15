"""Persistent export history — MongoDB-backed audit of dataset/batch exports."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase


class ExportHistoryRepository:
    """CRUD for export_history collection."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db["export_history"]

    async def create_record(self, record: Dict[str, Any]) -> str:
        now = datetime.utcnow()
        doc = {
            **record,
            "created_at": record.get("created_at", now),
            "updated_at": now,
        }
        result = await self.collection.insert_one(doc)
        return str(result.inserted_id)

    async def update_record(self, record_id: str, **fields: Any) -> bool:
        if not ObjectId.is_valid(record_id):
            return False
        fields["updated_at"] = datetime.utcnow()
        result = await self.collection.update_one(
            {"_id": ObjectId(record_id)},
            {"$set": fields},
        )
        return result.modified_count > 0

    async def update_by_job_id(self, job_id: str, **fields: Any) -> bool:
        fields["updated_at"] = datetime.utcnow()
        result = await self.collection.update_one(
            {"job_id": job_id},
            {"$set": fields},
        )
        return result.modified_count > 0

    async def get_by_id(self, record_id: str) -> Optional[Dict[str, Any]]:
        if not ObjectId.is_valid(record_id):
            return None
        return await self.collection.find_one({"_id": ObjectId(record_id)})

    async def list_for_user(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        dataset_name: Optional[str] = None,
        scope: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> Tuple[List[Dict], int]:
        query: Dict[str, Any] = {"user_id": str(user_id)}
        if dataset_name:
            query["dataset_name"] = dataset_name
        if scope in ("dataset", "batch"):
            query["scope"] = scope
        if search and search.strip():
            escaped = re.escape(search.strip())
            query["$or"] = [
                {"dataset_name": {"$regex": escaped, "$options": "i"}},
                {"filename": {"$regex": escaped, "$options": "i"}},
                {"format": {"$regex": escaped, "$options": "i"}},
            ]
        if date_from or date_to:
            date_q: Dict[str, Any] = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            query["created_at"] = date_q

        total = await self.collection.count_documents(query)
        skip = (page - 1) * page_size
        rows = (
            await self.collection.find(query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(page_size)
            .to_list(None)
        )
        return rows, total

    @staticmethod
    def serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(doc["_id"]),
            "user_id": doc.get("user_id"),
            "scope": doc.get("scope", "dataset"),
            "dataset_name": doc.get("dataset_name"),
            "batch_id": doc.get("batch_id"),
            "batch_number": doc.get("batch_number"),
            "format": doc.get("format"),
            "status": doc.get("status"),
            "filename": doc.get("filename"),
            "image_count": doc.get("image_count", 0),
            "split_enabled": doc.get("split_enabled", False),
            "job_id": doc.get("job_id"),
            "file_size": doc.get("file_size"),
            "error": doc.get("error"),
            "created_at": doc.get("created_at"),
            "completed_at": doc.get("completed_at"),
        }
