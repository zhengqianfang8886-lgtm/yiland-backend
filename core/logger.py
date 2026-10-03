import os
import sys
from loguru import logger

def setup_logging(level: str = "INFO"):
    """统一配置全局 Loguru 日志，支持彩色终端控制台与文件切片轮转"""
    logger.remove()
    
    # 确保日志输出目录存在
    os.makedirs("logs", exist_ok=True)

    console_format = (
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <7}</level> | "
        "<cyan>{extra[module]: <14}</cyan> | "
        "<level>{message}</level>"
    )
    logger.add(sys.stderr, format=console_format, level=level, colorize=True)
    logger.add(
        "logs/island_{time:YYYY-MM-DD}.log",
        rotation="10 MB",
        retention="3 days",
        compression="zip",
        level="DEBUG",
        enqueue=True,
        encoding="utf-8"
    )