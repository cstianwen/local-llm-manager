"""本地模型管家主窗口与导航外壳。"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..agent.engine import AgentEngine
from ..catalog.service import CatalogService
from ..core.models import OperationResult
from ..core.settings import Settings
from ..core.tasks import TaskManager
from ..providers.registry import create_default_registry
from ..services.downloads import DownloadService
from ..services.model_service import ModelService
from ..store.db import ModelStore
from .agent_page import AgentPage
from .model_page import ModelPage
from .resource_page import ResourcePage
from .settings_page import SettingsPage
from .task_status import TaskStatusWidget
from .theme import apply_theme


class MainWindow(QMainWindow):
    """公开核心服务对象，供后续场景、下载和设置页复用。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("本地模型管家")
        self.resize(1440, 920)
        self.setMinimumSize(1100, 760)

        self.store = ModelStore()
        self.registry = create_default_registry()
        self.service = ModelService(self.store, self.registry)
        self.tasks = TaskManager()
        self.settings = Settings(self.store)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, self.settings.all())
        self.catalog = CatalogService(store=self.store)
        self.download_service = DownloadService(self.store, self.registry, catalog=self.catalog)
        self.agent_engine = AgentEngine(
            provider=self.registry.get("ollama"),  # type: ignore[arg-type]
            store=self.store,
            fallback_provider=self.registry.get("t4"),
        )
        self.last_t4_running: bool | None = None
        self._allow_close = False
        self._close_stop_pending = False
        self._resources_closed = False
        self._sidebar_collapsed = False

        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(central)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(176)
        self.sidebar.setMaximumWidth(176)
        side_layout = QVBoxLayout(self.sidebar)
        side_layout.setContentsMargins(10, 12, 10, 10)
        side_layout.setSpacing(6)
        side_layout.addSpacing(6)
        self.collapse_button = QPushButton("收起侧栏")
        self.collapse_button.clicked.connect(self.toggle_sidebar)
        side_layout.addWidget(self.collapse_button)

        self.stack = QStackedWidget()
        self.model_page = ModelPage(self.store, self.registry, self.service, self.tasks)
        self.resource_page = ResourcePage(self.store, self.catalog, self.download_service, self.tasks)
        self.agent_page = AgentPage(self.agent_engine, self.tasks)
        self.settings_page = SettingsPage(
            self.store, self.settings, self.service, self.catalog, self.tasks,
            t4_panel=self.model_page.t4_panel,
        )
        self.pages: dict[str, QWidget] = {
            "models": self.model_page,
            "agent": self.agent_page,
            "downloads": self.resource_page,
            "settings": self.settings_page,
        }
        for key in ("models", "agent", "downloads", "settings"):
            self.stack.addWidget(self.pages[key])

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QToolButton] = {}
        for key, text, short in (
            ("models", "模型管理", "模"),
            ("agent", "AI 场景", "AI"),
            ("downloads", "资源下载", "下"),
            ("settings", "设置", "设"),
        ):
            button = QToolButton()
            button.setObjectName("navButton")
            button.setText(text)
            button.setToolTip(text)
            button.setCheckable(True)
            button.setProperty("full_text", text)
            button.setProperty("short_text", short)
            button.clicked.connect(lambda _checked=False, name=key: self.navigate_to(name))
            self.nav_group.addButton(button)
            self.nav_buttons[key] = button
            side_layout.addWidget(button)
        side_layout.addStretch(1)
        self.task_status = TaskStatusWidget()
        side_layout.addWidget(self.task_status)
        self.nav_buttons["models"].setChecked(True)

        outer.addWidget(self.sidebar)
        outer.addWidget(self.stack, 1)
        self.task_status.update_task("就绪", -1, "idle")
        self.model_page.navigation_requested.connect(self.navigate_to)
        self.model_page.task_status.connect(self._on_task_status)
        self.model_page.t4_state_changed.connect(self._on_t4_state)
        self.agent_page.status_message.connect(
            lambda message: self.task_status.update_task(message, -1, "ok")
        )
        self.agent_page.navigation_requested.connect(self.navigate_to)
        self.resource_page.task_status.connect(self._on_task_status)
        self.resource_page.navigation_requested.connect(self.navigate_to)
        self.resource_page.model_download_completed.connect(self.model_page.reload_models)
        self.settings_page.status_message.connect(
            lambda message: self.task_status.update_task(message, -1, "ok")
        )
        self.settings_page.settings_changed.connect(self._on_settings_changed)
        self.settings_page.models_changed.connect(self.model_page.reload_models)
        self.navigate_to("models")
        self._restore_window_preferences()

    def _on_task_status(self, text: str, percent: int, state: str) -> None:
        self.task_status.update_task(text, percent, state)

    def navigate_to(self, name: str) -> None:
        page = self.pages.get(name)
        if page is None:
            self.task_status.update_task(f"未知导航目标：{name}", -1, "error")
            return
        self.stack.setCurrentWidget(page)
        self.nav_buttons[name].setChecked(True)

    def _on_settings_changed(self, values: dict) -> None:
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, values)
        self.model_page.apply_settings(values)

    def _restore_window_preferences(self) -> None:
        encoded = self.settings.get("window_geometry", "")
        if isinstance(encoded, str) and encoded:
            try:
                self.restoreGeometry(QByteArray.fromBase64(QByteArray(encoded.encode("ascii"))))
            except (UnicodeError, ValueError):
                pass
        widths = self.settings.get("model_table_column_widths", [])
        if isinstance(widths, list) and len(widths) == self.model_page.table.columnCount():
            header = self.model_page.table.horizontalHeader()
            for column, value in enumerate(widths):
                if isinstance(value, int) and not isinstance(value, bool) and 40 <= value <= 2000:
                    header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
                    self.model_page.table.setColumnWidth(column, value)
        sizes = self.settings.get("model_splitter_sizes", [])
        if (
            isinstance(sizes, list) and len(sizes) == 2
            and all(isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 5000 for value in sizes)
        ):
            self.model_page.splitter.setSizes(sizes)
        if self.settings.get("sidebar_collapsed", False) is True:
            self.toggle_sidebar()

    def _save_window_preferences(self) -> None:
        table = self.model_page.table
        self.settings.update({
            "window_geometry": bytes(self.saveGeometry().toBase64().data()).decode("ascii"),
            "model_table_column_widths": [table.columnWidth(column) for column in range(table.columnCount())],
            "model_splitter_sizes": self.model_page.splitter.sizes(),
            "sidebar_collapsed": self._sidebar_collapsed,
        })

    def toggle_sidebar(self) -> None:
        self._sidebar_collapsed = not self._sidebar_collapsed
        width = 58 if self._sidebar_collapsed else 176
        self.sidebar.setMinimumWidth(width)
        self.sidebar.setMaximumWidth(width)
        self.collapse_button.setText("展开" if self._sidebar_collapsed else "收起侧栏")
        self.task_status.set_compact(self._sidebar_collapsed)
        for button in self.nav_buttons.values():
            button.setText(str(button.property("short_text" if self._sidebar_collapsed else "full_text")))
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextOnly
            )

    def _on_t4_state(self, running: object) -> None:
        self.last_t4_running = running if isinstance(running, bool) else None

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._allow_close and self.model_page.t4_running is True:
            event.ignore()
            if self._close_stop_pending:
                self.task_status.update_task("T4 停止任务仍在执行，等待真实结果后再退出。", -1, "running")
                return
            answer = QMessageBox.question(
                self,
                "退出前停止 T4",
                "最近已知 T4 服务仍在运行。退出前必须明确处理：\n\n"
                "是否执行 T4 停止脚本并等待真实停止结果？\n"
                "停止失败将保留主窗口，不会假装成功退出。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._close_stop_pending = True
            submitted = self.model_page.stop_t4(
                confirmed=True,
                report=False,
                on_complete=self._on_close_t4_stopped,
            )
            if not submitted:
                self._close_stop_pending = False
                QMessageBox.warning(
                    self,
                    "暂时不能退出",
                    "T4 停止任务未能提交（可能已有相同任务）。请等待任务结束后再关闭。",
                )
            return

        self._save_window_preferences()
        self.model_page.shutdown()
        self.resource_page.shutdown()
        self.agent_page.shutdown()
        self.settings_page.shutdown()
        # 只请求取消；SQLite / Provider 在事件循环退出后由 finish_shutdown 等待真实结束后再关。
        self.tasks.cancel_all()
        if self.tasks.active_keys:
            self.task_status.update_task(
                f"正在结束后台任务（{len(self.tasks.active_keys)} 个）…", -1, "running",
            )
        event.accept()

    def finish_shutdown(self) -> None:
        """在事件循环退出后等待协作任务结束，再安全释放共享资源。"""
        self.model_page.shutdown()
        self.resource_page.shutdown()
        self.agent_page.shutdown()
        self.settings_page.shutdown()
        self.tasks.cancel_all()
        # 不设超时：worker 仍在写库时必须等它真正退出，避免关闭后的数据库操作。
        self.tasks.wait_for_done()
        if not self._resources_closed:
            self._resources_closed = True
            self.registry.close()
            self.store.close()

    def _on_close_t4_stopped(self, result: OperationResult) -> None:
        self._close_stop_pending = False
        if not result.success:
            QMessageBox.critical(
                self,
                "T4 未停止，未退出",
                result.message or result.error or "停止失败，主窗口保持打开。",
            )
            return
        self._allow_close = True
        self.close()
