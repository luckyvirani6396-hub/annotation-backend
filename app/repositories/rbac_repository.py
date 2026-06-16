"""Repository classes for RBAC and task distribution data access."""

import re
from datetime import datetime
from typing import List, Optional, Dict, Any
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.schemas.rbac import (
    UserRole,
    TaskStatus,
    AnnotationStatus,
)


class UserRepository:
    """Repository for user operations with RBAC support."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db["users"]

    async def create_user(
        self,
        email: str,
        password_hash: str,
        full_name: str,
        role: UserRole = UserRole.ANNOTATOR,
        department: Optional[str] = None,
    ) -> str:
        """Create new user with role."""
        user = {
            "email": email,
            "password_hash": password_hash,
            "full_name": full_name,
            "role": role.value,
            "department": department,
            "is_active": True,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "last_login_at": None,
        }
        result = await self.collection.insert_one(user)
        return str(result.inserted_id)

    async def get_user_by_id(self, user_id: str) -> Optional[Dict]:
        """Get user by ID."""
        if not ObjectId.is_valid(user_id):
            return None
        return await self.collection.find_one({"_id": ObjectId(user_id)})

    async def get_user_by_email(self, email: str) -> Optional[Dict]:
        """Get user by email."""
        return await self.collection.find_one({"email": email})

    async def get_user_role(self, user_id: str) -> Optional[UserRole]:
        """Get user's role."""
        user = await self.get_user_by_id(user_id)
        if user and "role" in user:
            return UserRole(user["role"])
        return None

    async def update_user(self, user_id: str, **fields) -> bool:
        """Update user fields."""
        if not ObjectId.is_valid(user_id):
            return False
        
        update_data = {k: v for k, v in fields.items() if v is not None}
        update_data["updated_at"] = datetime.utcnow()

        result = await self.collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )
        return result.modified_count > 0

    async def update_user_role(self, user_id: str, role: UserRole) -> bool:
        """Update user's role (admin only)."""
        return await self.update_user(user_id, role=role.value)

    async def list_users(
        self,
        page: int = 1,
        page_size: int = 50,
        role: Optional[UserRole] = None,
        is_active: Optional[bool] = None,
    ) -> tuple[List[Dict], int]:
        """List users with pagination and optional filtering."""
        query = {}
        if role:
            query["role"] = role.value
        if is_active is not None:
            query["is_active"] = is_active

        total = await self.collection.count_documents(query)
        skip = (page - 1) * page_size

        users = await self.collection.find(query).skip(skip).limit(page_size).to_list(None)
        return users, total

    async def deactivate_user(self, user_id: str) -> bool:
        """Deactivate user."""
        return await self.update_user(user_id, is_active=False)

    async def activate_user(self, user_id: str) -> bool:
        """Activate user."""
        return await self.update_user(user_id, is_active=True)

    async def delete_user(self, user_id: str) -> bool:
        """Permanently delete a user document."""
        if not ObjectId.is_valid(user_id):
            return False
        result = await self.collection.delete_one({"_id": ObjectId(user_id)})
        return result.deleted_count > 0


class TaskBatchRepository:
    """Repository for task batch operations."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db["task_batches"]

    async def create_batch(
        self,
        project_id: str,
        batch_number: int,
        start_index: int,
        end_index: int,
        image_count: int,
        deadline: Optional[datetime] = None,
    ) -> str:
        """Create new task batch."""
        batch = {
            "project_id": project_id,
            "batch_number": batch_number,
            "start_index": start_index,
            "end_index": end_index,
            "image_count": image_count,
            "status": TaskStatus.PENDING.value,
            "assigned_to": None,
            "assigned_date": None,
            "deadline": deadline,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = await self.collection.insert_one(batch)
        return str(result.inserted_id)

    async def get_batch_by_id(self, batch_id: str) -> Optional[Dict]:
        """Get batch by ID."""
        if not ObjectId.is_valid(batch_id):
            return None
        return await self.collection.find_one({"_id": ObjectId(batch_id)})

    async def get_batches_by_project(
        self,
        project_id: str,
        page: int = 1,
        page_size: int = 50,
        status: Optional[TaskStatus] = None,
    ) -> tuple[List[Dict], int]:
        """List batches for project."""
        query = {"project_id": project_id}
        if status:
            if status == TaskStatus.SUBMITTED:
                query["status"] = {"$in": [TaskStatus.SUBMITTED.value, "annotated", "under_review"]}
            elif status == TaskStatus.REWORK:
                query["status"] = {"$in": [TaskStatus.REWORK.value, "rejected"]}
            else:
                query["status"] = status.value

        total = await self.collection.count_documents(query)
        skip = (page - 1) * page_size

        batches = await self.collection.find(query).skip(skip).limit(page_size).to_list(None)
        return batches, total

    async def get_batches_by_annotator(
        self,
        annotator_id: str,
        status: Optional[TaskStatus] = None,
    ) -> List[Dict]:
        """Get batches assigned to annotator."""
        query = {"assigned_to": annotator_id}
        if status:
            if status == TaskStatus.SUBMITTED:
                query["status"] = {"$in": [TaskStatus.SUBMITTED.value, "annotated", "under_review"]}
            elif status == TaskStatus.REWORK:
                query["status"] = {"$in": [TaskStatus.REWORK.value, "rejected"]}
            else:
                query["status"] = status.value
        return await self.collection.find(query).to_list(None)

    async def assign_batch(
        self,
        batch_id: str,
        annotator_id: str,
        due_date: Optional[datetime] = None,
    ) -> bool:
        """Assign batch to annotator."""
        if not ObjectId.is_valid(batch_id):
            return False

        result = await self.collection.update_one(
            {"_id": ObjectId(batch_id)},
            {
                "$set": {
                    "assigned_to": annotator_id,
                    "assigned_date": datetime.utcnow(),
                    "due_date": due_date,
                    "status": TaskStatus.ASSIGNED.value,
                    "updated_at": datetime.utcnow(),
                }
            }
        )
        return result.modified_count > 0

    async def update_batch_status(self, batch_id: str, status: TaskStatus) -> bool:
        """Update batch status."""
        if not ObjectId.is_valid(batch_id):
            return False

        result = await self.collection.update_one(
            {"_id": ObjectId(batch_id)},
            {
                "$set": {
                    "status": status.value,
                    "updated_at": datetime.utcnow(),
                }
            }
        )
        return result.modified_count > 0

    async def reassign_batch(self, batch_id: str, annotator_id: str) -> bool:
        """Reassign batch to different annotator."""
        return await self.assign_batch(batch_id, annotator_id)

    async def update_batch_fields(self, batch_id: str, **fields) -> bool:
        """Update arbitrary fields on a batch document."""
        if not ObjectId.is_valid(batch_id):
            return False
        fields["updated_at"] = datetime.utcnow()
        result = await self.collection.update_one(
            {"_id": ObjectId(batch_id)},
            {"$set": fields},
        )
        return result.modified_count > 0

    async def get_batches_for_review(self, statuses: Optional[List[str]] = None) -> List[Dict]:
        """Get all batches with given statuses (default: submitted/under_review)."""
        if statuses is None:
            statuses = [TaskStatus.SUBMITTED.value, TaskStatus.UNDER_REVIEW.value, "annotated"]
        return await self.collection.find({"status": {"$in": statuses}}).sort("updated_at", -1).to_list(None)

    async def list_all_batches(
        self,
        page: int = 1,
        page_size: int = 50,
        status: Optional[str] = None,
        project_id: Optional[str] = None,
        project_ids: Optional[List[str]] = None,
        search: Optional[str] = None,
    ) -> tuple:
        """List all batches across all projects (admin view)."""
        query: Dict[str, Any] = {}
        if status:
            if status == "submitted":
                query["status"] = {"$in": ["submitted", "annotated", "under_review"]}
            elif status == "rework":
                query["status"] = {"$in": ["rework", "rejected"]}
            else:
                query["status"] = status
        if project_id:
            query["project_id"] = project_id
        elif project_ids is not None:
            query["project_id"] = {"$in": project_ids}
        if search:
            escaped = re.escape(search.strip())
            or_clauses: List[Dict[str, Any]] = [
                {"project_id": {"$regex": escaped, "$options": "i"}},
            ]
            try:
                or_clauses.append({"batch_number": int(search.strip())})
            except ValueError:
                pass
            query["$or"] = or_clauses
        total = await self.collection.count_documents(query)
        skip = (page - 1) * page_size
        batches = await self.collection.find(query).sort("created_at", -1).skip(skip).limit(page_size).to_list(None)
        return batches, total


class AnnotationReviewRepository:
    """Repository for annotation review audit trail."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db["annotation_reviews"]

    async def create_review(
        self,
        annotation_id: str,
        reviewer_id: str,
        action: str,  # approve, reject
        previous_status: AnnotationStatus,
        new_status: AnnotationStatus,
        reason: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> str:
        """Create review audit entry."""
        review = {
            "annotation_id": annotation_id,
            "reviewer_id": reviewer_id,
            "action": action,
            "previous_status": previous_status.value,
            "new_status": new_status.value,
            "reason": reason,
            "notes": notes,
            "created_at": datetime.utcnow(),
        }
        result = await self.collection.insert_one(review)
        return str(result.inserted_id)

    async def get_review_history(self, annotation_id: str) -> List[Dict]:
        """Get review history for annotation."""
        return await self.collection.find(
            {"annotation_id": annotation_id}
        ).sort("created_at", -1).to_list(None)

    async def get_reviewer_activity(
        self,
        reviewer_id: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[Dict]:
        """Get reviewer's activity."""
        query = {"reviewer_id": reviewer_id}
        if start_date or end_date:
            query["created_at"] = {}
            if start_date:
                query["created_at"]["$gte"] = start_date
            if end_date:
                query["created_at"]["$lte"] = end_date

        return await self.collection.find(query).sort("created_at", -1).to_list(None)


class PermissionRepository:
    """Repository for project-level permissions."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db["permissions"]

    async def set_permission(
        self,
        user_id: str,
        project_id: str,
        role: UserRole,
        can_annotate: bool = False,
        can_review: bool = False,
        can_export: bool = False,
        can_manage_users: bool = False,
        can_create_batches: bool = False,
    ) -> str:
        """Set or update user permissions on project."""
        existing = await self.collection.find_one({
            "user_id": user_id,
            "project_id": project_id,
        })

        perm_doc = {
            "user_id": user_id,
            "project_id": project_id,
            "role": role.value,
            "can_annotate": can_annotate,
            "can_review": can_review,
            "can_export": can_export,
            "can_manage_users": can_manage_users,
            "can_create_batches": can_create_batches,
            "updated_at": datetime.utcnow(),
        }

        if existing:
            perm_doc["created_at"] = existing["created_at"]
            await self.collection.update_one(
                {"_id": existing["_id"]},
                {"$set": perm_doc}
            )
            return str(existing["_id"])
        else:
            perm_doc["created_at"] = datetime.utcnow()
            result = await self.collection.insert_one(perm_doc)
            return str(result.inserted_id)

    async def get_permissions(self, user_id: str, project_id: str) -> Optional[Dict]:
        """Get user's permissions on project."""
        return await self.collection.find_one({
            "user_id": user_id,
            "project_id": project_id,
        })

    async def has_permission(
        self,
        user_id: str,
        project_id: str,
        permission: str,
    ) -> bool:
        """Check if user has specific permission."""
        perm = await self.get_permissions(user_id, project_id)
        if not perm:
            return False
        return perm.get(permission, False)

    async def get_user_projects(self, user_id: str) -> List[Dict]:
        """Get all projects user has access to."""
        return await self.collection.find({"user_id": user_id}).to_list(None)


class AuditLogRepository:
    """Repository for audit logging."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db["audit_logs"]

    async def log_action(
        self,
        user_id: str,
        action: str,
        resource_type: str,
        resource_id: Optional[str] = None,
        changes: Optional[Dict] = None,
        ip_address: Optional[str] = None,
    ) -> str:
        """Log an action to audit trail."""
        log_entry = {
            "user_id": user_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "changes": changes,
            "ip_address": ip_address,
            "created_at": datetime.utcnow(),
        }
        result = await self.collection.insert_one(log_entry)
        return str(result.inserted_id)

    async def get_logs(
        self,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[List[Dict], int]:
        """Get audit logs with filtering."""
        query = {}
        if user_id:
            query["user_id"] = user_id
        if action:
            query["action"] = action
        if resource_type:
            query["resource_type"] = resource_type
        if resource_id:
            query["resource_id"] = resource_id

        if start_date or end_date:
            query["created_at"] = {}
            if start_date:
                query["created_at"]["$gte"] = start_date
            if end_date:
                query["created_at"]["$lte"] = end_date

        total = await self.collection.count_documents(query)
        skip = (page - 1) * page_size

        logs = await self.collection.find(query).skip(skip).limit(page_size).sort(
            "created_at", -1
        ).to_list(None)
        return logs, total
