# TrainingEdge — FIT parsing + training metrics computation

"""TrainingEdge 核心包。

导入时自动配置日志系统：
- 控制台输出（INFO 级别以上）
- 滚动日志文件（state/training_edge.log，5MB 上限，保留 3 个备份）
- 日志级别可通过环境变量 TRAININGEDGE_LOG_LEVEL 配置
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _setup_logging() -> None:
    """配置全局日志。"""
    log_level_name = os.environ.get("TRAININGEDGE_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    # 日志格式: 时间 | 级别 | 模块 | 消息
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 根日志器
    root_logger = logging.getLogger("training_edge")
    root_logger.setLevel(log_level)

    # 避免重复添加 handler（模块被多次导入时）
    if root_logger.handlers:
        return

    # 控制台 handler
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(fmt)
    root_logger.addHandler(console)

    # 滚动文件 handler
    log_dir = Path(__file__).resolve().parents[1] / "state"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "training_edge.log"

    file_handler = RotatingFileHandler(
        str(log_file),
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)


_setup_logging()
