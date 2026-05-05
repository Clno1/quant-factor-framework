"""
统一日志封装：控制台 + 文件双输出。

用法：
    from src.utils.logger import get_logger
    log = get_logger(__name__)
    log.info("hello")
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from src.config import CONFIG, PROJECT_ROOT

_LOGGERS: dict[str, logging.Logger] = {}

_FMT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _resolve_log_file() -> Path:
    try:
        log_dir = CONFIG.logging.log_dir
        file_name = CONFIG.logging.file_name
    except Exception:
        log_dir, file_name = "logs", "quant.log"
    path = Path(log_dir)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path / file_name


def _resolve_level() -> int:
    try:
        name = str(CONFIG.logging.level).upper()
    except Exception:
        name = "INFO"
    return getattr(logging, name, logging.INFO)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取带控制台与文件双输出的 logger，幂等。"""
    logger_name = name or "quant"
    if logger_name in _LOGGERS:
        return _LOGGERS[logger_name]

    logger = logging.getLogger(logger_name)
    logger.setLevel(_resolve_level())
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(_FMT, _DATE_FMT)

        # 控制台
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        # 文件（10MB 轮转，最多 5 份）
        try:
            fh = RotatingFileHandler(
                _resolve_log_file(),
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        except Exception as e:  # 允许不能写文件时也能运行
            logger.warning("File logging disabled: %s", e)

    _LOGGERS[logger_name] = logger
    return logger


__all__ = ["get_logger"]
