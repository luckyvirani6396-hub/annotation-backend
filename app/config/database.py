from motor.motor_asyncio import AsyncIOMotorClient
from app.config.settings import settings
from loguru import logger

class MongoDBManager:
    """MongoDB connection manager"""
    _instance = None
    client = None
    database = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    async def connect(self):
        """Connect to MongoDB"""
        try:
            self.client = AsyncIOMotorClient(settings.MONGODB_URL)
            self.database = self.client[settings.DATABASE_NAME]
            # Test connection
            await self.client.admin.command('ping')
            logger.success(f"Connected to MongoDB at {settings.MONGODB_URL}")
            return self.database
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise
    
    async def disconnect(self):
        """Disconnect from MongoDB"""
        if self.client:
            self.client.close()
            logger.info("Disconnected from MongoDB")
    
    def get_db(self):
        """Get database instance"""
        if self.database is None:
            raise Exception("Database not connected. Call connect() first.")
        return self.database
    
    def get_collection(self, name: str):
        """Get collection by name"""
        return self.get_db()[name]

# Global instance
db_manager = MongoDBManager()

async def init_db():
    """Initialize database connection"""
    return await db_manager.connect()

async def close_db():
    """Close database connection"""
    await db_manager.disconnect()

def get_database():
    """Get database instance"""
    return db_manager.get_db()

def get_collection(name: str):
    """Get collection instance"""
    return db_manager.get_collection(name)