"""集中配置管理 — 所有可配置值统一从此模块获取。

优先级: 环境变量 > 数据库 settings 表 > 默认值
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / "state"


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------

@dataclass
class TrainingEdgeConfig:
    """TrainingEdge 全局配置。"""

    # 训练参数
    ftp: float = 200.0                      # 功能阈值功率 (W)
    max_hr: int = 190                       # 最大心率 (bpm)
    resting_hr: int = 50                    # 静息心率 (bpm)

    # 存储路径
    db_path: Path = field(default_factory=lambda: STATE_DIR / "training_edge.db")
    fit_files_dir: Path = field(default_factory=lambda: STATE_DIR / "fit_files")
    log_file: Path = field(default_factory=lambda: STATE_DIR / "training_edge.log")

    # Web 服务
    host: str = "0.0.0.0"
    port: int = 8420

    # 日志
    log_level: str = "INFO"

    # Intervals.icu 集成
    intervals_api_key: Optional[str] = None
    intervals_athlete_id: Optional[str] = None
    intervals_base_url: str = "https://intervals.icu/api/v1"
    intervals_api_key_path: Path = field(
        default_factory=lambda: STATE_DIR / "intervals_api_key.txt"
    )

    # Garmin Connect
    garmin_token_path: Path = field(
        default_factory=lambda: STATE_DIR / "garmin_token.json"
    )

    def ensure_dirs(self) -> None:
        """确保所有必要目录存在。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.fit_files_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 环境变量映射
# ---------------------------------------------------------------------------

_ENV_MAP = {
    "TRAININGEDGE_FTP":                   ("ftp", float),
    "TRAININGEDGE_MAX_HR":                ("max_hr", int),
    "TRAININGEDGE_RESTING_HR":            ("resting_hr", int),
    "TRAININGEDGE_DB_PATH":               ("db_path", Path),
    "TRAININGEDGE_FIT_DIR":               ("fit_files_dir", Path),
    "TRAININGEDGE_HOST":                  ("host", str),
    "TRAININGEDGE_PORT":                  ("port", int),
    "TRAININGEDGE_LOG_LEVEL":             ("log_level", str),
    "TRAININGEDGE_LOG_FILE":              ("log_file", Path),
    "TRAININGEDGE_INTERVALS_API_KEY":     ("intervals_api_key", str),
    "TRAININGEDGE_INTERVALS_ATHLETE_ID":  ("intervals_athlete_id", str),
    "TRAININGEDGE_INTERVALS_BASE_URL":    ("intervals_base_url", str),
    "TRAININGEDGE_GARMIN_TOKEN_PATH":     ("garmin_token_path", Path),
}


def _apply_env(cfg: TrainingEdgeConfig) -> None:
    """用环境变量覆盖默认配置。"""
    for env_var, (attr, converter) in _ENV_MAP.items():
        value = os.environ.get(env_var)
        if value is not None:
            setattr(cfg, attr, converter(value))


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_config: Optional[TrainingEdgeConfig] = None


def get_config() -> TrainingEdgeConfig:
    """获取全局配置（单例模式）。

    首次调用时创建配置对象，应用环境变量覆盖，并确保目录存在。
    后续调用返回同一实例。

    Returns:
        TrainingEdgeConfig 实例
    """
    global _config
    if _config is None:
        _config = TrainingEdgeConfig()
        _apply_env(_config)
        _config.ensure_dirs()
    return _config


def reset_config() -> None:
    """重置配置单例（用于测试）。"""
    global _config
    _config = None
