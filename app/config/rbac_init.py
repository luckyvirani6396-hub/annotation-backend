"""Database initialization with MongoDB collections and indexes for RBAC system."""

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import OperationFailure
from loguru import logger


async def _create_index_safe(collection, index_spec, **kwargs):
    """Safely create an index, handling conflicts gracefully."""
    try:
        await collection.create_index(index_spec, **kwargs)
    except OperationFailure as e:
        if e.code == 85:  # IndexOptionsConflict
            logger.debug(f"Index already exists: {e}")
        else:
            raise


async def init_rbac_collections(db: AsyncIOMotorDatabase):
    """Initialize RBAC collections and indexes."""

    try:
        # Get list of existing collections
        collections = await db.list_collection_names()

        # ─── users collection ───────────────────────────────────────────────

        if "users" not in collections:
            await db.create_collection("users")
            logger.info("Created 'users' collection")
        else:
            logger.debug("'users' collection already exists")

        # Create indexes on users
        await _create_index_safe(db["users"], "email", unique=True)
        await _create_index_safe(db["users"], "role")
        await _create_index_safe(db["users"], "is_active")
        await _create_index_safe(db["users"], [("created_at", -1)])
        logger.debug("Created indexes on 'users' collection")

        # ─── task_batches collection ────────────────────────────────────────

        if "task_batches" not in collections:
            await db.create_collection("task_batches")
            logger.info("Created 'task_batches' collection")
        else:
            logger.debug("'task_batches' collection already exists")

        # Create indexes on task_batches
        await _create_index_safe(db["task_batches"], "project_id")
        await _create_index_safe(db["task_batches"], [("project_id", 1), ("batch_number", 1)], unique=True)
        await _create_index_safe(db["task_batches"], "assigned_to")
        await _create_index_safe(db["task_batches"], "status")
        await _create_index_safe(db["task_batches"], [("project_id", 1), ("status", 1)])
        await _create_index_safe(db["task_batches"], [("created_at", -1)])
        logger.debug("Created indexes on 'task_batches' collection")

        # ─── task_assignments collection ────────────────────────────────────

        if "task_assignments" not in collections:
            await db.create_collection("task_assignments")
            logger.info("Created 'task_assignments' collection")
        else:
            logger.debug("'task_assignments' collection already exists")

        # Create indexes on task_assignments
        await _create_index_safe(db["task_assignments"], "batch_id")
        await _create_index_safe(db["task_assignments"], "annotator_id")
        await _create_index_safe(db["task_assignments"], "status")
        await _create_index_safe(db["task_assignments"], [("annotator_id", 1), ("status", 1)])
        await _create_index_safe(db["task_assignments"], [("batch_id", 1), ("annotator_id", 1)], unique=True)
        await _create_index_safe(db["task_assignments"], [("assigned_date", -1)])
        logger.debug("Created indexes on 'task_assignments' collection")

        # ─── annotation_reviews collection ──────────────────────────────────

        if "annotation_reviews" not in collections:
            await db.create_collection("annotation_reviews")
            logger.info("Created 'annotation_reviews' collection")
        else:
            logger.debug("'annotation_reviews' collection already exists")

        # Create indexes on annotation_reviews
        await _create_index_safe(db["annotation_reviews"], "annotation_id")
        await _create_index_safe(db["annotation_reviews"], "reviewer_id")
        await _create_index_safe(db["annotation_reviews"], "action")
        await _create_index_safe(db["annotation_reviews"], [("annotation_id", 1), ("created_at", -1)])
        await _create_index_safe(db["annotation_reviews"], [("reviewer_id", 1), ("created_at", -1)])
        logger.debug("Created indexes on 'annotation_reviews' collection")

        # ─── permissions collection ─────────────────────────────────────────

        if "permissions" not in collections:
            await db.create_collection("permissions")
            logger.info("Created 'permissions' collection")
        else:
            logger.debug("'permissions' collection already exists")

        # Create indexes on permissions
        await _create_index_safe(db["permissions"], "user_id")
        await _create_index_safe(db["permissions"], "project_id")
        await _create_index_safe(db["permissions"], [("user_id", 1), ("project_id", 1)], unique=True)
        await _create_index_safe(db["permissions"], "role")
        logger.debug("Created indexes on 'permissions' collection")

        # ─── audit_logs collection ──────────────────────────────────────────

        if "audit_logs" not in collections:
            await db.create_collection("audit_logs")
            logger.info("Created 'audit_logs' collection")
        else:
            logger.debug("'audit_logs' collection already exists")

        # Create indexes on audit_logs
        await _create_index_safe(db["audit_logs"], "user_id")
        await _create_index_safe(db["audit_logs"], "resource_type")
        await _create_index_safe(db["audit_logs"], "action")
        await _create_index_safe(db["audit_logs"], [("user_id", 1), ("created_at", -1)])
        await _create_index_safe(db["audit_logs"], [("resource_type", 1), ("resource_id", 1)])
        await _create_index_safe(db["audit_logs"], [("created_at", -1)])
        # TTL index: auto-delete logs older than 90 days
        await _create_index_safe(db["audit_logs"], "created_at", expireAfterSeconds=90*24*60*60)
        logger.debug("Created indexes on 'audit_logs' collection")

        # ─── export_history collection ───────────────────────────────────────

        if "export_history" not in collections:
            await db.create_collection("export_history")
            logger.info("Created 'export_history' collection")
        else:
            logger.debug("'export_history' collection already exists")

        await _create_index_safe(db["export_history"], "user_id")
        await _create_index_safe(db["export_history"], "dataset_name")
        await _create_index_safe(db["export_history"], "batch_id")
        await _create_index_safe(db["export_history"], "job_id")
        await _create_index_safe(db["export_history"], "status")
        await _create_index_safe(db["export_history"], [("user_id", 1), ("created_at", -1)])
        await _create_index_safe(db["export_history"], [("created_at", -1)])
        logger.debug("Created indexes on 'export_history' collection")

        # ─── update annotations collection for workflow states ───────────────

        if "annotations" in collections:
            # Add indexes for new fields
            await _create_index_safe(db["annotations"], "status")
            await _create_index_safe(db["annotations"], [("image_id", 1), ("status", 1)])
            await _create_index_safe(db["annotations"], "annotator_id")
            await _create_index_safe(db["annotations"], "reviewed_by")
            logger.debug("Updated indexes on 'annotations' collection")

        logger.success("RBAC collections and indexes initialized successfully")

    except Exception as e:
        logger.error(f"Failed to initialize RBAC collections: {e}")
        raise


async def init_enhanced_users_schema(db: AsyncIOMotorDatabase):
    """
    Enhance existing users collection with new fields.
    This migrates existing users to have role field.
    """
    try:
        users_collection = db["users"]

        # Add role field to existing users without one (default to ADMIN for creator, ANNOTATOR for others)
        result = await users_collection.update_many(
            {"role": {"$exists": False}},
            {
                "$set": {
                    "role": "admin",  # Existing users become admin by default
                    "department": None,
                }
            }
        )

        if result.modified_count > 0:
            logger.info(f"Enhanced {result.modified_count} existing users with role field")

        # Drop the legacy ``is_admin`` boolean now that authorization is
        # role-based exclusively.
        legacy = await users_collection.update_many(
            {"is_admin": {"$exists": True}},
            {"$unset": {"is_admin": ""}},
        )
        if legacy.modified_count > 0:
            logger.info(f"Removed legacy is_admin field from {legacy.modified_count} users")

    except Exception as e:
        logger.error(f"Failed to enhance users schema: {e}")
        raise
