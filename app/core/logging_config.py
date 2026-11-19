"""应用日志与崩溃采集：UTF-8 滚动文件，重复初始化不叠加处理器。"""
from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import traceback
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


_CRASH_FILES = []
_QT_HANDLER_INSTALLED = False


def setup_crash_handlers(
    file_path: str | Path | None = None,
) -> Path:
    """安装原生崩溃与未捕获异常记录，避免进程静默消失。

    - ``faulthandler`` 在 C 层崩溃（如 Qt6Core 访问冲突）时输出 Python 栈；
    - ``sys.excepthook`` / ``threading.excepthook`` 记录未捕获 Python 异常；
    - Qt 的 qWarning / qFatal 消息写入同一文件。
    """
    path = Path(file_path) if file_path is not None else log_path().with_name("crash.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, "a", encoding="utf-8", buffering=1)
    _CRASH_FILES.append(stream)
    faulthandler.enable(stream, all_threads=True)

    logger = logging.getLogger("app.crash")

    def excepthook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        stream.write(f"\n[未捕获异常] {text}")
        stream.flush()
        logger.error("未捕获异常：%s", text)

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        stream.write(f"\n[线程 {getattr(args.thread, 'name', '?')} 未捕获异常] {text}")
        stream.flush()
        logger.error("线程未捕获异常：%s", text)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    _install_qt_message_handler(stream, logger)
    return path


def _install_qt_message_handler(stream, logger) -> None:
    global _QT_HANDLER_INSTALLED
    if _QT_HANDLER_INSTALLED:
        return
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return

    def handler(mode, _context, message: str) -> None:
        level = {
            QtMsgType.QtDebugMsg: "DEBUG",
            QtMsgType.QtInfoMsg: "INFO",
            QtMsgType.QtWarningMsg: "WARNING",
            QtMsgType.QtCriticalMsg: "CRITICAL",
            QtMsgType.QtFatalMsg: "FATAL",
        }.get(mode, "MESSAGE")
        stream.write(f"\n[Qt {level}] {message}")
        stream.flush()
        if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            logger.warning("Qt %s：%s", level, message)

    qInstallMessageHandler(handler)
    _QT_HANDLER_INSTALLED = True
