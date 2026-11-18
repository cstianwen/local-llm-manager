"""应用日志配置：UTF-8 滚动文件，重复初始化不叠加处理器。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LOG_BACKUP_COUNT, LOG_MAX_BYTES, log_path


def setup_logging(
    file_path: str | Path | None = None,
    level: int = logging.INFO,
) -> Path:
    path = Path(file_path) if file_path is not None else log_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("app")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_model_manager_handler", False):
            logger.removeHandler(handler)
            handler.close()

    handler = RotatingFileHandler(
        path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    handler._model_manager_handler = True
    logger.addHandler(handler)
    return path
