"""本地模型管家应用入口。"""
from __future__ import annotations

import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app.core.config import APP_NAME, APP_VERSION
from app.core.logging_config import setup_crash_handlers, setup_logging
from app.ui.main_window import MainWindow

ICON_RELATIVE_PATH = os.path.join("assets", "app.ico")


def resource_path(relative: str) -> str:
    """兼容源码运行与 PyInstaller 打包后的资源路径。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def load_app_icon() -> QIcon:
    path = resource_path(ICON_RELATIVE_PATH)
    if not os.path.exists(path):
        return QIcon()
    return QIcon(path)


def main() -> int:
    # 必须在 QApplication 之前打开崩溃记录，否则原生崩溃来不及写日志。
    setup_logging()
    setup_crash_handlers()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("LocalModelManager")
    app.setWindowIcon(load_app_icon())
    # 主题只在 MainWindow 初始化时按已保存设置应用一次，避免默认值覆盖用户选择。
    window = MainWindow()
    window.show()
    exit_code = app.exec()
    window.finish_shutdown()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
