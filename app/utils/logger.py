from loguru import logger
from app.config.logging_config import setup_logging

# Ensure logging is configured
setup_logging()

def get_logger(name: str = None):
    """Get configured logger instance"""
    if name:
        return logger.bind(name=name)
    return logger