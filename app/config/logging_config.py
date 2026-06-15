import sys
from loguru import logger
from app.config.settings import settings

def setup_logging():
    """Configure logging with loguru"""
    logger.remove()
    
    # Console logging
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=settings.LOG_LEVEL,
        colorize=True
    )
    
    # File logging
    logger.add(
        f"{settings.LOG_DIR}/app.log",
        rotation="500 MB",
        retention="10 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level=settings.LOG_LEVEL
    )
    
    # Error logging
    logger.add(
        f"{settings.LOG_DIR}/error.log",
        rotation="100 MB",
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="ERROR"
    )
    
    # Success logging
    logger.add(
        f"{settings.LOG_DIR}/success.log",
        rotation="500 MB",
        retention="10 days",
        filter=lambda record: record["level"].name == "SUCCESS",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}"
    )
    
    logger.info("Logging configured successfully")
    return logger