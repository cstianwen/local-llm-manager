"""资源目录、确认式下载与真实任务进度页面。"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..catalog.resources import CatalogResource
from ..catalog.service import CatalogService, CatalogSnapshot
from ..core.models import DownloadProgress, OperationResult, human_size
from ..core.phases import phase_text
from ..core.tasks import Task, TaskManager
from ..services.downloads import DownloadService
from ..store.db import ModelStore
from .dispatch import GuiDispatch


_MODEL_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*")
_REFRESH_KEY = "resource:catalog-refresh"
_TASK_ROLE = int(Qt.ItemDataRole.UserRole)
_STATUS_TEXT = {
    "pending": "等待中",
    "downloading": "下载中",
    "cancelling": "取消中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def _status_text(status: Any) -> str:
    value = str(status or "pending")
    return _STATUS_TEXT.get(value, value)


def _bytes_text(value: Any, *, unknown: str = "未知") -> str:
    try:
        number = max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return unknown
    return human_size(number) if number else unknown


class ResourceCard(QFrame):
    """筛选时保留实例，避免切换目录导致任务进度丢失。"""

    action_requested = Signal(object)
    install_plan_requested = Signal(object)
    open_directory_requested = Signal(object)
    cancel_requested = Signal(object)

    def __init__(self, resource: CatalogResource, category_name: str):
        super().__init__()
        self.resource = resource
        self._task_key = ""
        self._registered = False
        self._downloaded = False
        self.setObjectName("resourceCard")
        self.setStyleSheet(
            "QFrame#resourceCard { background: #1b222e; border: 1px solid #334057; border-radius: 7px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(7)

        heading = QHBoxLayout()
        self.name_label = QLabel()
        self.name_label.setStyleSheet("font-size: 16px; font-weight: 700; border: none;")
        self.name_label.setWordWrap(True)
        heading.addWidget(self.name_label, 1)
        self.state_label = QLabel()
        self.state_label.setStyleSheet("color: #8dbdff; font-weight: 600; border: none;")
        self.state_label.setWordWrap(True)
        heading.addWidget(self.state_label)
        layout.addLayout(heading)

        self.category_label = QLabel()
        self.category_label.setStyleSheet("color: #aebcd0; border: none;")
        layout.addWidget(self.category_label)
        self.description_label = QLabel()
        self.description_label.setWordWrap(True)
        self.description_label.setStyleSheet("border: none;")
        layout.addWidget(self.description_label)
        self.metadata_label = QLabel()
        self.metadata_label.setWordWrap(True)
        self.metadata_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.metadata_label.setStyleSheet("color: #b9c5d6; border: none;")
        layout.addWidget(self.metadata_label)
        self.network_label = QLabel()
        self.network_label.setWordWrap(True)
        self.network_label.setStyleSheet("border: none;")
        layout.addWidget(self.network_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_label = QLabel()
        self.progress_label.setWordWrap(True)
        self.progress_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.progress_label.setStyleSheet("border: none;")
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)

        buttons = QHBoxLayout()
        self.action_button = QPushButton()
        self.action_button.clicked.connect(lambda: self.action_requested.emit(self.resource))
        self.plan_button = QPushButton("确认安装计划")
        self.plan_button.clicked.connect(lambda: self.install_plan_requested.emit(self.resource))
        self.directory_button = QPushButton("打开所在目录")
        self.directory_button.clicked.connect(lambda: self.open_directory_requested.emit(self.resource))
        self.cancel_button = QPushButton("取消当前任务")
        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(self._task_key))
        buttons.addWidget(self.action_button)
        buttons.addWidget(self.plan_button)
        buttons.addWidget(self.directory_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.cancel_button.setVisible(False)
        self.set_resource(resource, category_name)

    def set_resource(self, resource: CatalogResource, category_name: str) -> None:
        self.resource = resource
        self.name_label.setText(resource.display_name)
        self.category_label.setText(f"分类：{category_name}（{resource.category}）｜类型：{resource.kind}｜交付：{resource.delivery}")
        self.description_label.setText(resource.description)
        size = human_size(resource.size_bytes) if resource.size_bytes > 0 else "未知（官方未声明）"
        tags = "、".join(resource.tags) if resource.tags else "无"
        self.metadata_label.setText(f"标签：{tags}\n许可证：{resource.license or '见官方页面'}\n已知大小：{size}")
        network: list[str] = []
        if resource.online_required:
            network.append("下载需联网")
        else:
            network.append("首次获取无需联网")
        if not resource.offline_capable:
            network.append("需联网，不可纯离线")
            self.network_label.setStyleSheet("color: #ff8585; font-weight: 700; border: none;")
        else:
            if resource.delivery == "builtin":
                network.append("内置可离线")
            elif resource.kind == "model" and resource.delivery == "ollama":
                network.append("获取后可离线")
            else:
                network.append("获取并配置后可离线")
            self.network_label.setStyleSheet("color: #83d5a5; border: none;")
        self.network_label.setText("｜".join(network))
        self._update_actions()

    def update_state(self, *, registered: bool, downloaded: bool, task_key: str = "") -> None:
        self._registered = registered
        self._downloaded = downloaded
        self._task_key = task_key
        self._update_actions()

    def _update_actions(self) -> None:
        resource = self.resource
        busy = bool(self._task_key)
        self.plan_button.setVisible(False)
        self.directory_button.setVisible(False)
        self.action_button.setVisible(True)
        self.action_button.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        if resource.delivery == "builtin":
            self.state_label.setText("已内置")
            self.action_button.setText("打开AI场景")
        elif resource.delivery == "official-page":
            self.state_label.setText("仅官方页面，不是安装包")
            self.action_button.setText("打开官方页面")
            self.action_button.setEnabled(not busy and bool(resource.official_url or resource.source_url))
        elif resource.delivery == "ollama":
            self.state_label.setText("已登记（Ollama）" if self._registered else "可拉取")
            self.action_button.setText("重新拉取" if self._registered else "确认并拉取")
        elif resource.delivery == "https-file":
            if self._downloaded:
                self.state_label.setText("已下载（不代表已安装）")
                self.action_button.setText("已下载（不代表已安装）")
                self.action_button.setEnabled(False)
                self.directory_button.setVisible(True)
                self.plan_button.setVisible(resource.requires_install)
            else:
                self.state_label.setText("尚未下载")
                self.action_button.setText("确认并下载")
        else:
            self.state_label.setText("不支持的交付方式")
            self.action_button.setText("不可下载")
            self.action_button.setEnabled(False)

    def show_progress(self, info: dict[str, Any]) -> None:
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)
        total = int(info.get("total_bytes") or 0)
        downloaded = int(info.get("downloaded_bytes") or 0)
        status = str(info.get("status") or "pending")
        active = bool(info.get("active"))
        if total > 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(min(100, round(downloaded * 100 / total)))
        elif active:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1 if status == "completed" else 0)
        speed = float(info.get("speed_bps") or 0.0)
        text = (
            f"{_status_text(status)}｜已下载 {human_size(max(0, downloaded))} / "
            f"总量 {human_size(total) if total else '未知'}｜速度 {human_size(max(0, speed))}/s"
        )
        details = str(info.get("details") or "")
        if details:
            text += f"\n{details}"
        self.progress_label.setText(text)
        self.progress_label.setStyleSheet(
            f"color: {'#ff8585' if status == 'failed' else '#b9c5d6'}; border: none;"
        )


class ResourcePage(QWidget):
    status_message = Signal(str)
    navigation_requested = Signal(str)
    model_download_completed = Signal(object)
    task_status = Signal(str, int, str)

    def __init__(
        self,
        store: ModelStore,
        catalog: CatalogService,
        download_service: DownloadService,
        tasks: TaskManager,
    ):
        super().__init__()
        self.store = store
        self.catalog = catalog
        self.download_service = download_service
        self.tasks = tasks
        self.snapshot = CatalogSnapshot(cache_path=str(catalog.cache_path))
        self._resources: dict[str, CatalogResource] = {}
        self._cards: dict[str, ResourceCard] = {}
        self._categories = {item.id: item for item in catalog.categories()}
        self._registered_refs: set[str] = set()
        self._downloaded_paths: dict[str, str] = {}
        self._task_info: dict[str, dict[str, Any]] = {}
        self._session_order: list[str] = []
        self._active_keys: list[str] = []
        self._resource_tasks: dict[str, str] = {}
        self._model_tasks: dict[str, str] = {}
        self._shutting_down = False
        self._dispatch = GuiDispatch(self)
        self._build_ui()
        self._refresh_registered_models()
        self._apply_snapshot(catalog.snapshot())
        self._load_download_history()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(8)
        title_row = QHBoxLayout()
        title = QLabel("资源下载")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.refresh_button = QPushButton("刷新官方库")
        self.refresh_button.clicked.connect(self.refresh_official_catalog)
        title_row.addWidget(self.refresh_button)
        root.addLayout(title_row)

        self.source_label = QLabel("浏览和筛选不会联网；只有确认“刷新官方库”后才访问官方地址，失败会回退到内置目录。")
        self.source_label.setWordWrap(True)
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source_label.setStyleSheet("color: #b9c5d6;")
        root.addWidget(self.source_label)

        controls = QHBoxLayout()
        self.kind_combo = QComboBox()
        for text, value in (("全部类型", ""), ("大模型", "model"), ("Agent组件", "agent"), ("配套软件", "software")):
            self.kind_combo.addItem(text, value)
        controls.addWidget(self.kind_combo)
        self.category_combo = QComboBox()
        self.category_combo.addItem("全部分类", "")
        for category in self._categories.values():
            self.category_combo.addItem(category.name, category.id)
            self.category_combo.setItemData(
                self.category_combo.count() - 1, category.description, Qt.ItemDataRole.ToolTipRole,
            )
        controls.addWidget(self.category_combo)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索名称、描述或标签……")
        controls.addWidget(self.search_edit, 1)
        root.addLayout(controls)
        self.kind_combo.currentIndexChanged.connect(self._apply_filters)
        self.category_combo.currentIndexChanged.connect(self._apply_filters)
        self.search_edit.textChanged.connect(self._apply_filters)

        manual_row = QHBoxLayout()
        self.manual_edit = QLineEdit()
        self.manual_edit.setPlaceholderText("手动输入 Ollama model:tag，例如 qwen2.5:3b")
        manual_row.addWidget(self.manual_edit, 1)
        self.manual_button = QPushButton("确认并拉取")
        self.manual_button.clicked.connect(self.pull_manual_model)
        self.manual_edit.returnPressed.connect(self.pull_manual_model)
        manual_row.addWidget(self.manual_button)
        root.addLayout(manual_row)

        body = QHBoxLayout()
        body.setSpacing(10)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        resource_container = QWidget()
        resource_layout = QVBoxLayout(resource_container)
        resource_layout.setContentsMargins(0, 0, 0, 0)
        self.count_label = QLabel("0 项资源")
        resource_layout.addWidget(self.count_label)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.cards_widget = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_widget)
        self.cards_layout.setContentsMargins(0, 0, 4, 0)
        self.cards_layout.setSpacing(9)
        self.empty_label = QLabel("没有符合当前筛选条件的资源。")
        self.empty_label.setVisible(False)
        self.cards_layout.addWidget(self.empty_label)
        self.cards_layout.addStretch(1)
        self.scroll_area.setWidget(self.cards_widget)
        resource_layout.addWidget(self.scroll_area, 1)
        self.splitter.addWidget(resource_container)

        activity_box = QGroupBox("活动任务 / 最近下载（真实记录）")
        activity_layout = QVBoxLayout(activity_box)
        current_row = QHBoxLayout()
        self.current_task_label = QLabel("暂无活动任务；历史记录来自 SQLite。")
        self.current_task_label.setWordWrap(True)
        current_row.addWidget(self.current_task_label, 1)
        self.cancel_button = QPushButton("取消当前任务")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_current_task)
        current_row.addWidget(self.cancel_button)
        activity_layout.addLayout(current_row)
        self.history_table = QTableWidget(0, 6)
        self.history_table.setHorizontalHeaderLabels(("资源 / 模型", "状态", "已下载", "总量", "速度", "目标或真实错误"))
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.verticalHeader().setVisible(False)
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        activity_layout.addWidget(self.history_table)
        self.splitter.addWidget(activity_box)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([500, 210])
        body.addWidget(self.splitter, 1)
        root.addLayout(body, 1)

    def _apply_snapshot(self, snapshot: CatalogSnapshot) -> None:
        self.snapshot = snapshot
        resources = list(snapshot.resources)
        self._resources = {item.id: item for item in resources}
        present = set(self._resources)
        for resource_id in list(self._cards):
            if resource_id not in present and resource_id not in self._resource_tasks:
                card = self._cards.pop(resource_id)
                self.cards_layout.removeWidget(card)
                card.deleteLater()
        for index, resource in enumerate(resources):
            card = self._cards.get(resource.id)
            if card is None:
                card = ResourceCard(resource, self._category_name(resource.category))
                self._cards[resource.id] = card
                card.action_requested.connect(self._handle_card_action)
                card.install_plan_requested.connect(self.confirm_installation_plan)
                card.open_directory_requested.connect(self.open_download_directory)
                card.cancel_requested.connect(self._cancel_task)
            else:
                card.set_resource(resource, self._category_name(resource.category))
            self.cards_layout.removeWidget(card)
            self.cards_layout.insertWidget(index, card)
        self._refresh_card_states()
        self._apply_filters()
        source_names = {"builtin": "内置离线目录", "cache": "本地官方库缓存", "refreshed": "本次官方库刷新"}
        refreshed = (
            datetime.fromtimestamp(snapshot.refreshed_at).strftime("%Y-%m-%d %H:%M:%S")
            if snapshot.refreshed_at else "尚无官方刷新时间"
        )
        state = "；缓存过期或刷新未完全成功" if snapshot.stale else ""
        errors = "\n真实错误：" + "\n".join(snapshot.errors) if snapshot.errors else ""
        self.source_label.setText(
            f"当前来源：{source_names.get(snapshot.source, snapshot.source)}；最近刷新：{refreshed}{state}\n"
            f"缓存文件：{snapshot.cache_path or self.catalog.cache_path}\n"
            "初始化、列举与筛选不联网；仅确认后的官方库刷新会联网，失败回退内置目录或已有缓存。"
            f"{errors}"
        )

    def _category_name(self, category_id: str) -> str:
        category = self._categories.get(category_id)
        return category.name if category else category_id or "未分类"

    def _apply_filters(self, *_args: Any) -> None:
        category = str(self.category_combo.currentData() or "")
        kind = str(self.kind_combo.currentData() or "")
        query = self.search_edit.text().strip().casefold()
        visible = 0
        for resource_id, card in self._cards.items():
            resource = self._resources.get(resource_id, card.resource)
            haystack = " ".join((
                resource.id, resource.display_name, resource.description,
                resource.model_ref, resource.license, *resource.tags,
            )).casefold()
            matches = (
                (not category or resource.category == category)
                and (not kind or resource.kind == kind)
                and (not query or query in haystack)
            )
            card.setVisible(matches)
            visible += int(matches)
        self.empty_label.setVisible(visible == 0)
        self.count_label.setText(f"显示 {visible} / {len(self._resources)} 项资源")

    def _refresh_registered_models(self) -> None:
        self._registered_refs = {
            model.model_ref.strip().casefold()
            for model in self.store.all()
            if model.model_ref.strip()
        }

    def _target_path(self, resource: CatalogResource) -> Path:
        known = self._downloaded_paths.get(resource.id, "")
        if known:
            return Path(known)
        return self.download_service.download_dir / (resource.filename or f"{resource.id}.download")

    def _refresh_card_states(self) -> None:
        for resource_id, card in self._cards.items():
            resource = card.resource
            target = self._target_path(resource)
            downloaded = resource.delivery == "https-file" and target.is_file()
            card.update_state(
                registered=bool(resource.model_ref and resource.model_ref.casefold() in self._registered_refs),
                downloaded=downloaded,
                task_key=self._resource_tasks.get(resource_id, ""),
            )

    def _handle_card_action(self, resource: CatalogResource) -> None:
        if resource.delivery == "builtin":
            self.navigation_requested.emit("agent")
        elif resource.delivery == "official-page":
            self.open_official_page(resource)
        elif resource.delivery in ("ollama", "https-file"):
            self.confirm_download(resource)

    def refresh_official_catalog(self) -> None:
        if self._shutting_down:
            return
        if _REFRESH_KEY in self._active_keys or _REFRESH_KEY in self.tasks.active_keys:
            self.status_message.emit("官方库刷新已在运行，本次未重复提交。")
            return
        answer = QMessageBox.question(
            self,
            "确认联网刷新官方库",
            "此操作会访问 Hugging Face 官方 HTTPS API 并写入本地缓存。\n"
            "失败会保留并回退到内置目录或已有缓存。是否确认联网刷新？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._submit_task(
            _REFRESH_KEY,
            self.catalog.refresh,
            name="官方资源库",
            kind="refresh",
            force=True,
        )

    def pull_manual_model(self) -> None:
        model_ref = self.manual_edit.text().strip()
        if not _MODEL_REF.fullmatch(model_ref):
            QMessageBox.warning(self, "模型名称格式无效", "请输入完整的 Ollama model:tag，且不要包含空白字符。")
            return
        resource = next(
            (item for item in self._resources.values() if item.model_ref.casefold() == model_ref.casefold()),
            None,
        )
        self._confirm_ollama(model_ref, resource)

    def confirm_download(self, resource: CatalogResource) -> None:
        if resource.delivery == "ollama":
            self._confirm_ollama(resource.model_ref, resource)
            return
        if resource.delivery != "https-file":
            return
        target = self._target_path(resource)
        if target.is_file():
            self.status_message.emit(f"目标文件已存在，未重复下载：{target}")
            self._refresh_card_states()
            return
        answer = QMessageBox.question(
            self,
            "确认联网下载文件",
            f"资源：{resource.display_name}\n官方文件地址：{resource.download_url}\n"
            f"保存位置：{target}\n\n将联网下载；完成后不会自动运行或安装。是否确认？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        key = f"resource:https-file:{resource.id}"
        self._submit_task(
            key,
            self.download_service.download_resource,
            resource.id,
            name=resource.display_name,
            kind="https-file",
            resource_id=resource.id,
        )

    def _confirm_ollama(self, model_ref: str, resource: CatalogResource | None) -> None:
        normalized = model_ref.strip().casefold()
        if not normalized:
            QMessageBox.warning(self, "模型名称为空", "目录条目没有可拉取的 Ollama model:tag。")
            return
        key = f"resource:ollama:{normalized}"
        if normalized in self._model_tasks or key in self.tasks.active_keys:
            self.status_message.emit(f"模型 {model_ref} 已有拉取任务，本次未重复提交。")
            return
        registered = normalized in self._registered_refs
        answer = QMessageBox.question(
            self,
            "确认通过 Ollama 联网拉取",
            f"模型：{model_ref}\n"
            f"当前本地登记状态：{'已登记，确认后重新拉取' if registered else '未登记，可拉取'}\n\n"
            "本工具只调用既有本机 Ollama API，不会启动服务；Ollama 通常还需联网访问模型仓库。\n"
            "是否明确确认拉取？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._submit_task(
            key,
            self.download_service.pull_ollama,
            model_ref,
            name=model_ref,
            kind="ollama",
            resource_id=resource.id if resource else "",
            model_ref=model_ref,
            resource_id_for_service=resource.id if resource else "",
        )

    def _submit_task(
        self,
        key: str,
        worker: Callable[..., Any],
        *args: Any,
        name: str,
        kind: str,
        resource_id: str = "",
        model_ref: str = "",
        resource_id_for_service: str | None = None,
        **worker_kwargs: Any,
    ) -> Task | None:
        if self._shutting_down:
            return None
        if key in self._active_keys or key in self.tasks.active_keys:
            self.status_message.emit(f"任务 {name} 已在运行，本次未重复提交。")
            return None
        if model_ref and model_ref.casefold() in self._model_tasks:
            self.status_message.emit(f"模型 {model_ref} 已有拉取任务，本次未重复提交。")
            return None
        if resource_id_for_service is not None:
            worker_kwargs["resource_id"] = resource_id_for_service
        info: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "resource_id": resource_id,
            "model_ref": model_ref,
            "status": "pending",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "speed_bps": 0.0,
            "details": "已提交，等待后台任务真实进度。",
            "active": True,
            "job_id": 0,
        }
        self._task_info[key] = info
        self._session_order.append(key)
        self._active_keys.append(key)
        if resource_id:
            self._resource_tasks[resource_id] = key
        if model_ref:
            self._model_tasks[model_ref.casefold()] = key
        task = self.tasks.submit(key, worker, *args, **worker_kwargs)
        if task is None:
            self._active_keys.remove(key)
            self._resource_tasks.pop(resource_id, None)
            if model_ref:
                self._model_tasks.pop(model_ref.casefold(), None)
            info.update(active=False, status="failed", details="共享 TaskManager 中已存在相同任务，未重复提交。")
            self._update_task_views(key)
            self.status_message.emit(info["details"])
            return None
        def deliver(callback: Callable[..., Any], *values: Any) -> None:
            if self._shutting_down:
                return

            def run() -> None:
                if not self._shutting_down:
                    callback(*values)

            self._dispatch.post(run)

        task.signals.progress.connect(lambda value: deliver(self._on_progress, key, value))
        task.signals.result.connect(lambda value: deliver(self._on_result, key, value))
        task.signals.error.connect(lambda error: deliver(self._on_error, key, error))
        task.signals.cancelled.connect(lambda: deliver(self._on_cancelled, key))
        task.signals.finished.connect(lambda: deliver(self._on_finished, key))
        if kind == "refresh":
            self.refresh_button.setEnabled(False)
        self._refresh_card_states()
        self._update_task_views(key)
        self.status_message.emit(f"已提交后台任务：{name}")
        self._set_task_status(f"已提交后台任务：{name}", -1, "running")
        return task

    def _set_task_status(self, text: str, percent: int = -1, state: str = "running") -> None:
        self.task_status.emit(text, percent, state)

    def _on_progress(self, key: str, value: Any) -> None:
        info = self._task_info[key]
        percent = -1
        if isinstance(value, DownloadProgress):
            info.update(
                status=value.status,
                downloaded_bytes=max(0, value.downloaded_bytes),
                total_bytes=max(0, value.total_bytes),
                speed_bps=max(0.0, value.speed_bps),
                details=value.error or "正在接收后台任务的真实进度。",
            )
            total = max(0, value.total_bytes)
            if total:
                percent = min(100, round(max(0, value.downloaded_bytes) * 100 / total))
        elif isinstance(value, dict):
            phase = phase_text(value.get("phase"))
            url = str(value.get("url") or "")
            info["details"] = f"{phase}{'：' + url if url else ''}"
            if str(value.get("phase") or "") == "completed":
                info["status"] = "completed"
        else:
            info["details"] = str(value)
        self._set_task_status(f"{info['name']} · {info['details']}", percent, "running")
        self._update_task_views(key)

    def _on_result(self, key: str, result: Any) -> None:
        info = self._task_info[key]
        if isinstance(result, CatalogSnapshot):
            info["status"] = "completed"
            info["details"] = (
                f"目录包含 {len(result.resources)} 项；来源：{result.source}。"
                + ("\n" + "\n".join(result.errors) if result.errors else "")
            )
            self._apply_snapshot(result)
            message = (
                "官方库刷新完成，但存在真实错误，已保留可用目录。" if result.errors else "官方库刷新完成。"
            )
            self.status_message.emit(message)
            self._set_task_status(message, -1, "error" if result.errors else "ok")
            return
        if not isinstance(result, OperationResult):
            info.update(status="failed", details=f"后台任务返回了无法识别的结果：{result!r}")
            self._update_task_views(key)
            return
        data = result.data if isinstance(result.data, dict) else {}
        info["job_id"] = int(data.get("job_id") or 0)
        info["status"] = "completed" if result.success else "failed"
        info["details"] = result.message if result.success else (result.error or result.message)
        resource_id = str(info.get("resource_id") or "")
        resource = self._resources.get(resource_id)
        if result.success and info["kind"] == "ollama":
            self._refresh_registered_models()
            self._refresh_card_states()
            self.model_download_completed.emit(str(info.get("model_ref") or ""))
            self.status_message.emit(f"{info['name']} 拉取完成；已重新读取模型登记缓存，未自动刷新 Provider。")
        elif result.success and info["kind"] == "https-file":
            path = str(data.get("path") or (self._target_path(resource) if resource else ""))
            if resource and path:
                self._downloaded_paths[resource.id] = path
            self._refresh_card_states()
            self.status_message.emit(f"文件下载完成，未执行：{path}")
            if resource and path:
                QTimer.singleShot(0, lambda item=resource, target=path: self._prompt_https_completed(item, target))
        elif not result.success:
            self.status_message.emit(f"{info['name']} 失败：{result.error or result.message}")
        self._set_task_status(
            f"{info['name']}：{_status_text(info['status'])}"
            + (f" · {info.get('details')}" if info.get("details") else ""),
            -1, "ok" if info["status"] == "completed" else "error",
        )
        self._update_task_views(key)

    def _on_error(self, key: str, error: Exception) -> None:
        info = self._task_info[key]
        message = str(error) or error.__class__.__name__
        info.update(status="failed", details=message)
        self._update_task_views(key)
        self.status_message.emit(f"{info['name']} 失败：{message}")
        self._set_task_status(f"{info['name']} 失败：{message}", -1, "error")

    def _on_cancelled(self, key: str) -> None:
        info = self._task_info[key]
        info.update(status="cancelled", details="后台任务已确认取消。", speed_bps=0.0)
        self._update_task_views(key)
        self.status_message.emit(f"任务已取消：{info['name']}")
        self._set_task_status(f"任务已取消：{info['name']}", -1, "cancelled")

    def _on_finished(self, key: str) -> None:
        info = self._task_info[key]
        info["active"] = False
        if key in self._active_keys:
            self._active_keys.remove(key)
        resource_id = str(info.get("resource_id") or "")
        if self._resource_tasks.get(resource_id) == key:
            self._resource_tasks.pop(resource_id, None)
        model_ref = str(info.get("model_ref") or "").casefold()
        if self._model_tasks.get(model_ref) == key:
            self._model_tasks.pop(model_ref, None)
        if key == _REFRESH_KEY:
            self.refresh_button.setEnabled(True)
        self._refresh_card_states()
        self._update_task_views(key)

    def _update_task_views(self, key: str) -> None:
        info = self._task_info[key]
        self._upsert_task_row(key, info)
        resource_id = str(info.get("resource_id") or "")
        card = self._cards.get(resource_id)
        if card:
            card.show_progress(info)
        self._refresh_activity_summary()

    def _refresh_activity_summary(self) -> None:
        key = self._active_keys[-1] if self._active_keys else (self._session_order[-1] if self._session_order else "")
        if key:
            info = self._task_info[key]
            prefix = "当前任务" if info.get("active") else "最近任务"
            self.current_task_label.setText(
                f"{prefix}：{info['name']}｜{_status_text(info['status'])}｜{info.get('details', '')}"
            )
        else:
            self.current_task_label.setText("暂无活动任务；历史记录来自本机缓存。")
        self.cancel_button.setEnabled(bool(self._active_keys) and not self._shutting_down)

    def _set_row(self, row: int, values: tuple[str, ...], task_key: str = "") -> None:
        for column, value in enumerate(values):
            item = self.history_table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self.history_table.setItem(row, column, item)
            item.setText(value)
            item.setToolTip(value)
            if column == 0:
                item.setData(_TASK_ROLE, task_key)

    def _upsert_task_row(self, key: str, info: dict[str, Any]) -> None:
        row = -1
        for candidate in range(self.history_table.rowCount()):
            item = self.history_table.item(candidate, 0)
            if item and item.data(_TASK_ROLE) == key:
                row = candidate
                break
        if row < 0:
            self.history_table.insertRow(0)
            row = 0
        self._set_row(row, (
            str(info["name"]),
            _status_text(info["status"]),
            human_size(max(0, int(info.get("downloaded_bytes") or 0))),
            _bytes_text(info.get("total_bytes")),
            f"{human_size(max(0, float(info.get('speed_bps') or 0.0)))}/s",
            str(info.get("details") or ""),
        ), key)

    def _load_download_history(self) -> None:
        try:
            records = self.download_service.list_downloads(limit=50)
        except Exception as exc:
            self.current_task_label.setText(f"下载历史读取失败：{exc}")
            self.status_message.emit(f"下载历史读取失败：{exc}")
            return
        for record in records:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            name = str(record.get("model_id") or record.get("resource_id") or "未命名资源")
            details = str(record.get("error") or record.get("integrity_error") or record.get("destination") or record.get("url") or "")
            self._set_row(row, (
                name,
                _status_text(record.get("status")),
                human_size(max(0, int(record.get("downloaded_bytes") or 0))),
                _bytes_text(record.get("total_bytes")),
                f"{human_size(max(0.0, float(record.get('speed_bps') or 0.0)))}/s",
                details,
            ))

    def cancel_current_task(self) -> None:
        key = ""
        row = self.history_table.currentRow()
        if row >= 0:
            item = self.history_table.item(row, 0)
            candidate = str(item.data(_TASK_ROLE) or "") if item else ""
            if candidate in self._active_keys:
                key = candidate
        if not key and self._active_keys:
            key = self._active_keys[-1]
        self._cancel_task(key)

    def _cancel_task(self, key: object) -> None:
        task_key = str(key or "")
        if task_key not in self._active_keys:
            return
        if self.tasks.cancel(task_key):
            info = self._task_info[task_key]
            info.update(status="cancelling", details="已请求取消，等待后台任务确认；尚未假装停止。")
            self._update_task_views(task_key)

    def open_official_page(self, resource: CatalogResource) -> None:
        url = resource.official_url or resource.source_url
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            QMessageBox.warning(self, "官方地址不可用", f"未打开无效或非 HTTPS 地址：{url}")
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "打开失败", f"系统未能打开官方页面：\n{url}")

    def _prompt_https_completed(self, resource: CatalogResource, path: str) -> None:
        if self._shutting_down:
            return
        if not resource.requires_install:
            QMessageBox.information(
                self,
                "下载完成，未执行",
                f"文件已下载，未执行任何内容：\n{path}\n官方来源：{resource.official_url or resource.source_url}",
            )
            return
        answer = QMessageBox.question(
            self,
            "下载完成，未执行",
            f"文件已下载，但尚未安装或执行：\n{path}\n\n"
            "是否再次确认生成手动安装计划？计划只展示真实路径与官方来源，绝不运行安装包。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._show_installation_plan(resource, path, confirmed=True)

    def confirm_installation_plan(self, resource: CatalogResource) -> None:
        path = str(self._target_path(resource))
        if not Path(path).is_file():
            QMessageBox.warning(self, "文件不存在", f"不能为不存在的文件生成安装计划：\n{path}")
            return
        answer = QMessageBox.question(
            self,
            "再次确认安装计划",
            f"只会生成计划，不会运行或安装：\n{path}\n是否确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._show_installation_plan(resource, path, confirmed=True)

    def _show_installation_plan(self, resource: CatalogResource, path: str, *, confirmed: bool) -> None:
        if not confirmed or self._shutting_down:
            return
        try:
            plan = self.download_service.installation_plan(resource.id, path, confirmation=True)
        except Exception as exc:
            QMessageBox.critical(self, "安装计划生成失败", str(exc))
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("手动安装计划（未执行）")
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setText(
            f"资源：{plan.display_name}\n真实文件路径：{plan.downloaded_path}\n"
            f"官方来源：{plan.official_url}\n\n"
            "仅生成计划，未执行任何安装。请自行通过系统界面安装，并核对文件与官方来源。"
        )
        dialog.setDetailedText(plan.message)
        open_button = dialog.addButton("打开所在目录", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()
        if dialog.clickedButton() is open_button:
            self.open_download_directory(resource)

    def open_download_directory(self, resource: CatalogResource) -> None:
        path = self._target_path(resource)
        folder = path.parent
        if not path.is_file() or not folder.is_dir():
            QMessageBox.warning(self, "目录不可用", f"已完成下载文件或所在目录不可访问：\n{path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(self, "打开失败", f"系统未能打开目录：\n{folder}")

    def shutdown(self) -> None:
        self._shutting_down = True
        for key in tuple(self._active_keys):
            self.tasks.cancel(key)
        self.cancel_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.manual_button.setEnabled(False)
