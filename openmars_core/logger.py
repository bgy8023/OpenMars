# 统一日志配置
import os
import sys
import re
from loguru import logger

def sanitize_message(message: str) -> str:
    """
    清理日志消息，移除敏感信息
    
    Args:
        message: 原始消息
        
    Returns:
        清理后的消息
    """
    if not isinstance(message, str):
        return message
    
    # 过滤 API Key
    message = re.sub(r'sk-[a-zA-Z0-9_-]{20,}', '[REDACTED_API_KEY]', message)
    message = re.sub(r'(API[-_]?KEY|api[-_]?key)\s*[=:]\s*[^\s]+', r'\1=[REDACTED]', message, flags=re.IGNORECASE)
    
    # 过滤密码
    message = re.sub(r'(password|PASSWORD)\s*[=:]\s*[^\s]+', r'\1=[REDACTED]', message, flags=re.IGNORECASE)
    
    # 过滤 Token
    message = re.sub(r'(token|TOKEN)\s*[=:]\s*[^\s]+', r'\1=[REDACTED]', message, flags=re.IGNORECASE)
    
    # 过滤 Bearer Token
    message = re.sub(r'Bearer\s+[a-zA-Z0-9_.-]{20,}', 'Bearer [REDACTED]', message)
    
    return message

# 保存原始的 logger 方法
original_debug = logger.debug
original_info = logger.info
original_warning = logger.warning
original_error = logger.error
original_critical = logger.critical

def safe_debug(message, *args, **kwargs):
    sanitized_msg = sanitize_message(str(message))
    original_debug(sanitized_msg, *args, **kwargs)

def safe_info(message, *args, **kwargs):
    sanitized_msg = sanitize_message(str(message))
    original_info(sanitized_msg, *args, **kwargs)

def safe_warning(message, *args, **kwargs):
    sanitized_msg = sanitize_message(str(message))
    original_warning(sanitized_msg, *args, **kwargs)

def safe_error(message, *args, **kwargs):
    sanitized_msg = sanitize_message(str(message))
    original_error(sanitized_msg, *args, **kwargs)

def safe_critical(message, *args, **kwargs):
    sanitized_msg = sanitize_message(str(message))
    original_critical(sanitized_msg, *args, **kwargs)

# 替换 logger 方法
logger.debug = safe_debug
logger.info = safe_info
logger.warning = safe_warning
logger.error = safe_error
logger.critical = safe_critical

# 日志目录
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# 清除默认配置
logger.remove()

# 控制台输出
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="INFO",
    enqueue=True
)

# 文件输出
logger.add(
    os.path.join(LOG_DIR, "app_{time:YYYY-MM-DD}.log"),
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    level="DEBUG",
    rotation="00:00",
    retention="30 days",
    compression="zip",
    enqueue=True
)

# 错误日志单独输出
logger.add(
    os.path.join(LOG_DIR, "error_{time:YYYY-MM-DD}.log"),
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    level="ERROR",
    rotation="00:00",
    retention="90 days",
    compression="zip",
    enqueue=True
)

__all__ = ["logger"]
