"""内置 Agent 场景页、独立聊天窗与确认式工作区窗口。"""
from __future__ import annotations

import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..agent.commands import CommandPlan
from ..agent.engine import AgentEngine
from ..agent.models import (
    AgentEvent,
    AgentEventType,
    EntryPoint,
    SceneDefinition,
    SceneKind,
    ScenePhase,
    SceneState,
)
from ..agent.protocol import SuggestionParseResult
from ..agent.workspace import ApplyResult, ChangeSet, FileContent, PendingChange, SearchResult
from ..core.tasks import Task, TaskManager


_PHASE_TEXT = {
    ScenePhase.IDLE: "未检查",
    ScenePhase.CHECKING: "检查中",
    ScenePhase.PROVIDER_OFFLINE: "模型服务未连接",
    ScenePhase.NEEDS_MODEL: "需要选择模型",
    ScenePhase.NEEDS_DOWNLOAD: "需要下载模型",
    ScenePhase.NEEDS_LOAD: "需要装入模型",
    ScenePhase.NEEDS_SOFTWARE: "需要配套软件",
    ScenePhase.NEEDS_WORKSPACE: "需要选择工作区",
    ScenePhase.READY: "已就绪",
    ScenePhase.STREAMING: "正在回复",
    ScenePhase.CANCELLED: "已取消",
    ScenePhase.ERROR: "错误",
}
_ITEM_ROLE = int(Qt.ItemDataRole.UserRole)


def _provider_text(value: Any) -> str:
    return {
        "ollama": "本机已有模型服务",
        "t4": "T4 后备服务",
        "llamacpp": "本地模型文件",
    }.get(str(value or "").casefold(), "本机模型服务")


def _phase(value: ScenePhase | str) -> ScenePhase:
    try:
        return value if isinstance(value, ScenePhase) else ScenePhase(value)
    except ValueError:
        return ScenePhase.ERROR


def _prepare_worker(
    token: Any,
    progress: Callable[[Any], None],
    engine: AgentEngine,
    scene_id: str,
    model: str,
    workspace_root: str,
) -> SceneState:
    token.raise_if_cancelled()
    state = engine.prepare_scene(
        scene_id, model=model, workspace_root=workspace_root or None,
    )
    token.raise_if_cancelled()
    return state


def _load_worker(
    token: Any,
    progress: Callable[[Any], None],
    engine: AgentEngine,
    scene_id: str,
    model: str,
) -> SceneState:
    token.raise_if_cancelled()
    state = engine.load_scene_model(scene_id, model)
    token.raise_if_cancelled()
    return state


def _stream_worker(
    token: Any,
    progress: Callable[[Any], None],
    engine: AgentEngine,
    session_id: str,
    prompt: str,
) -> AgentEvent:
    terminal = {
        AgentEventType.ASSISTANT_COMPLETED,
        AgentEventType.CANCELLED,
        AgentEventType.ERROR,
    }
    stream = engine.stream_reply(session_id, prompt)
    last: AgentEvent | None = None
    try:
        for event in stream:
            last = event
            progress(event)
            token.raise_if_cancelled()
            if event.type in terminal:
                return event
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    raise RuntimeError("回复流结束，但未收到完成、取消或错误事件")


def _search_worker(
    token: Any,
    progress: Callable[[Any], None],
    engine: AgentEngine,
    session_id: str,
    query: str,
    content: bool,
    pattern: str,
) -> SearchResult:
    token.raise_if_cancelled()
    result = engine.search_workspace(
        session_id, query, content=content, pattern=pattern or "*",
    )
    token.raise_if_cancelled()
    return result


def _read_worker(
    token: Any,
    progress: Callable[[Any], None],
    engine: AgentEngine,
    session_id: str,
    path: str,
) -> FileContent:
    token.raise_if_cancelled()
    result = engine.read_workspace_file(session_id, path)
    token.raise_if_cancelled()
    return result


def _preview_worker(
    token: Any,
    progress: Callable[[Any], None],
    engine: AgentEngine,
    session_id: str,
    message_id: str,
) -> tuple[ChangeSet, SuggestionParseResult]:
    token.raise_if_cancelled()
    result = engine.preview_model_response(session_id, message_id=message_id)
    token.raise_if_cancelled()
    return result


def _apply_worker(
    token: Any,
    progress: Callable[[Any], None],
    engine: AgentEngine,
    session_id: str,
    change_id: str,
) -> ApplyResult:
    token.raise_if_cancelled()
    confirmation = engine.request_change_confirmation(session_id, change_id)
    token.raise_if_cancelled()
    return engine.apply_change(session_id, change_id, confirmation)


def _companion_plan_worker(
    token: Any,
    progress: Callable[[Any], None],
    engine: AgentEngine,
    scene_id: str,
    model: str,
) -> list[CommandPlan]:
    token.raise_if_cancelled()
    plans = engine.create_software_start_plans(scene_id, model=model)
    token.raise_if_cancelled()
    return plans


class _EngineEventBridge(QObject):
    event_received = Signal(object)


@dataclass(frozen=True)
class _TaskDelivery:
    key: str
    kind: str
    value: Any = None


class _AgentTaskOwner:
    """把引擎事件跨线程排队，并在所有者销毁时断开任务回调。"""

    def _init_agent_owner(
        self,
        engine: AgentEngine,
        tasks: TaskManager,
        event_handler: Callable[[AgentEvent], None],
    ) -> None:
        self.engine = engine
        self.tasks = tasks
        self._closing = False
        self._active_task_keys: set[str] = set()
        self._owned_tasks: dict[str, Task] = {}
        self._task_connections: dict[str, list[tuple[Any, Callable[..., Any]]]] = {}
        self._task_handlers: dict[str, dict[str, Callable[..., Any] | None]] = {}
        self._event_bridge = _EngineEventBridge()
        self._engine_event_handler = event_handler
        self._dispatch_handler = self._dispatch_owner_event
        self._event_bridge.event_received.connect(self._dispatch_handler)
        self._unsubscribe = engine.subscribe(self._event_bridge.event_received.emit)
        owner_ref = weakref.ref(self)

        def destroyed(*_args: Any) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._shutdown_agent_owner()

        self.destroyed.connect(destroyed)

    def _submit_task(
        self,
        key: str,
        worker: Callable[..., Any],
        *args: Any,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_progress: Callable[[Any], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
        **worker_kwargs: Any,
    ) -> Task | None:
        if self._closing:
            return None
        if key in self._active_task_keys:
            self.status_message.emit(f"任务已在运行：{key}")
            return None
        start = threading.Event()

        def gated_worker(token: Any, progress: Callable[[Any], None], *worker_args: Any, **options: Any) -> Any:
            start.wait()
            return worker(token, progress, *worker_args, **options)

        task = self.tasks.submit(key, gated_worker, *args, **worker_kwargs)
        if task is None:
            self.status_message.emit(f"共享任务管理器中已有相同任务：{key}")
            return None
        self._active_task_keys.add(key)
        # 保留 Task 本体，直到其结果与 finished 均在 GUI 线程分发完毕。
        self._owned_tasks[key] = task
        self._task_handlers[key] = {
            "progress": on_progress,
            "result": on_result,
            "error": on_error,
            "cancelled": on_cancelled,
            "finished": on_finished,
        }
        connections = self._task_connections.setdefault(key, [])
        bridge = self._event_bridge

        def deliver(kind: str, value: Any = None) -> None:
            bridge.event_received.emit(_TaskDelivery(key, kind, value))

        def connect(signal: Any, callback: Callable[..., Any]) -> None:
            signal.connect(callback)
            connections.append((signal, callback))

        try:
            connect(task.signals.progress, lambda value: deliver("progress", value))
            connect(task.signals.result, lambda value: deliver("result", value))
            connect(task.signals.error, lambda error: deliver("error", error))
            connect(task.signals.cancelled, lambda: deliver("cancelled"))
            connect(task.signals.finished, lambda: deliver("finished"))
        finally:
            # TaskManager.submit 会立即启动线程；先接信号，再放行快速任务。
            start.set()
        return task

    @Slot(object)
    def _dispatch_owner_event(self, payload: Any) -> None:
        if self._closing:
            return
        if not isinstance(payload, _TaskDelivery):
            handler = getattr(self, "_engine_event_handler", None)
            if handler is not None:
                handler(payload)
            return
        handlers = self._task_handlers.get(payload.key)
        if handlers is None:
            return
        callback = handlers.get(payload.kind)
        if payload.kind == "finished":
            self._active_task_keys.discard(payload.key)
            self._disconnect_task(payload.key)
            if callback is not None:
                callback()
        elif payload.kind == "cancelled":
            if callback is not None:
                callback()
            else:
                self.status_message.emit(f"任务已取消：{payload.key}")
        elif payload.kind == "error" and callback is None:
            self._default_task_error(payload.key, payload.value)
        elif callback is not None:
            callback(payload.value)

    def _default_task_error(self, key: str, error: Exception) -> None:
        self.status_message.emit(f"任务 {key} 失败：{error}")

    def _disconnect_task(self, key: str) -> None:
        self._task_handlers.pop(key, None)
        self._owned_tasks.pop(key, None)
        for signal, callback in self._task_connections.pop(key, []):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError, SystemError):
                pass

    def _cancel_task(self, key: str) -> bool:
        if key not in self._active_task_keys:
            return False
        return self.tasks.cancel(key)

    def _shutdown_agent_owner(self) -> None:
        self._closing = True
        unsubscribe = getattr(self, "_unsubscribe", None)
        if callable(unsubscribe):
            self._unsubscribe = None
            try:
                unsubscribe()
            except (RuntimeError, TypeError, SystemError):
                pass
        # Qt 会自动断开正在销毁的接收者；这里只退订引擎与任务，避免销毁期二次断连。
        self._event_bridge = None
        self._engine_event_handler = None
        self._dispatch_handler = None
        for key in tuple(getattr(self, "_active_task_keys", ())):
            self.tasks.cancel(key)
        for key in tuple(getattr(self, "_task_connections", {})):
            self._disconnect_task(key)
        getattr(self, "_active_task_keys", set()).clear()
        getattr(self, "_task_handlers", {}).clear()


class SceneCard(QFrame):
    prepare_requested = Signal(object)
    load_requested = Signal(object)
    enter_requested = Signal(object)

    def __init__(self, scene: SceneDefinition, state: SceneState):
        super().__init__()
        self.scene = scene
        self.state = state
        self.setObjectName("agentSceneCard")
        self.setStyleSheet(
            "QFrame#agentSceneCard { background: #1b222e; border: 1px solid #334057; "
            "border-radius: 7px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(7)
        title_row = QHBoxLayout()
        name = QLabel(scene.name)
        name.setStyleSheet("font-size: 17px; font-weight: 700; border: none;")
        title_row.addWidget(name, 1)
        self.phase_label = QLabel()
        self.phase_label.setStyleSheet("color: #8dbdff; font-weight: 600; border: none;")
        title_row.addWidget(self.phase_label)
        layout.addLayout(title_row)
        description = QLabel(scene.description or "自定义内置 Agent 场景")
        description.setWordWrap(True)
        description.setStyleSheet("border: none;")
        layout.addWidget(description)
        self.network_label = QLabel(
            "离线：模型就绪后的内置对话与文件工具｜联网：仅用户明确确认的模型/资源下载"
        )
        if scene.entry_point == EntryPoint.COMPANION_SOFTWARE:
            self.network_label.setText(
                "离线：内置策划对话｜联网：仅显式资源下载；配套软件只生成计划，不自动启动"
            )
        self.network_label.setWordWrap(True)
        self.network_label.setStyleSheet("color: #83d5a5; border: none;")
        layout.addWidget(self.network_label)
        self.model_label = QLabel()
        self.model_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.model_label.setStyleSheet("border: none;")
        layout.addWidget(self.model_label)
        self.availability_label = QLabel("可用状态：尚未检测")
        self.availability_label.setStyleSheet("color: #b9c5d6; border: none;")
        layout.addWidget(self.availability_label)
        self.detail_label = QLabel()
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_label.setStyleSheet("color: #b9c5d6; border: none;")
        layout.addWidget(self.detail_label)
        buttons = QHBoxLayout()
        self.prepare_button = QPushButton("检查真实状态")
        self.prepare_button.clicked.connect(lambda: self.prepare_requested.emit(self.scene))
        self.load_button = QPushButton("明确装入模型")
        self.load_button.clicked.connect(lambda: self.load_requested.emit(self.scene))
        self.enter_button = QPushButton("进入")
        self.enter_button.clicked.connect(lambda: self.enter_requested.emit(self.scene))
        buttons.addWidget(self.prepare_button)
        buttons.addWidget(self.load_button)
        buttons.addStretch(1)
        buttons.addWidget(self.enter_button)
        layout.addLayout(buttons)
        self.set_state(state)

    def set_state(self, state: SceneState) -> None:
        self.state = state
        phase = _phase(state.phase)
        self.phase_label.setText(_PHASE_TEXT[phase])
        model = state.model or self.scene.model
        self.model_label.setText(f"推荐模型：{model or '未指定（进入后填写本机已有模型）'}")
        ready = phase == ScenePhase.READY
        self.availability_label.setText(
            "可用状态：可以开始对话" if ready
            else "可用状态：需要先检查或装入推荐模型"
            if phase in {ScenePhase.NEEDS_LOAD, ScenePhase.PROVIDER_OFFLINE, ScenePhase.IDLE}
            else f"可用状态：{_PHASE_TEXT[phase]}"
        )
        self.availability_label.setStyleSheet(
            f"color: {'#83d5a5' if ready else '#d8b56a'}; border: none;"
        )
        detail = state.error or state.message
        if not detail and phase == ScenePhase.IDLE:
            detail = "尚未检测；构造页面不会访问网络、装入模型或启动服务。"
        provider = state.data.get("active_provider") or state.provider.get("active_provider")
        if provider:
            label = _provider_text(provider)
            detail = f"{detail}\n当前模型服务：{label}" if detail else f"当前模型服务：{label}"
        self.detail_label.setText(detail)
        self.detail_label.setStyleSheet(
            f"color: {'#ff8585' if phase in {ScenePhase.ERROR, ScenePhase.PROVIDER_OFFLINE} else '#b9c5d6'}; "
            "border: none;"
        )
        self.load_button.setVisible(phase == ScenePhase.NEEDS_LOAD and bool(model))

    def show_task_error(self, message: str) -> None:
        self.phase_label.setText("任务失败")
        self.detail_label.setText(message)
        self.detail_label.setStyleSheet("color: #ff8585; border: none;")

    def set_busy(self, busy: bool) -> None:
        self.prepare_button.setEnabled(not busy)
        self.load_button.setEnabled(not busy)


class AgentPage(_AgentTaskOwner, QWidget):
    status_message = Signal(str)
    navigation_requested = Signal(str)

    def __init__(self, engine: AgentEngine, tasks: TaskManager, parent: QWidget | None = None):
        super().__init__(parent)
        self._cards: dict[str, SceneCard] = {}
        self._scene_tasks: dict[str, str] = {}
        self._windows: dict[str, AgentChatWindow] = {}
        self._init_agent_owner(engine, tasks, self._on_engine_event)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        title_row = QHBoxLayout()
        title = QLabel("AI 场景")
        title.setStyleSheet("font-size: 23px; font-weight: 700;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        models_button = QPushButton("模型管理")
        models_button.clicked.connect(lambda: self.navigation_requested.emit("models"))
        downloads_button = QPushButton("资源下载")
        downloads_button.clicked.connect(lambda: self.navigation_requested.emit("downloads"))
        title_row.addWidget(models_button)
        title_row.addWidget(downloads_button)
        root.addLayout(title_row)
        note = QLabel(
            "全部 AI 回复都由本工具内置引擎产生；打开页面不会检测、下载或装入模型，"
            "也不会启动任何推理服务或外部编程工具。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #b9c5d6;")
        root.addWidget(note)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self.cards_layout = QVBoxLayout(content)
        self.cards_layout.setContentsMargins(0, 0, 4, 0)
        self.cards_layout.setSpacing(10)
        scenes = self.engine.scenes.list(include_custom=True)
        for scene in scenes:
            card = SceneCard(scene, self.engine.get_scene_state(scene.id))
            card.prepare_requested.connect(self._prepare_scene)
            card.load_requested.connect(self._load_scene_model)
            card.enter_requested.connect(self._open_scene)
            self._cards[scene.id] = card
            self.cards_layout.addWidget(card)
        self.cards_layout.addStretch(1)
        self.scroll_area.setWidget(content)
        root.addWidget(self.scroll_area, 1)
        self.summary_label = QLabel(
            f"已列出 {len(scenes)} 个场景（含已有自定义场景）；状态均来自后端，未进行自动检测。"
        )
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

    def _on_engine_event(self, event: AgentEvent) -> None:
        if self._closing or event.type != AgentEventType.STATE:
            return
        card = self._cards.get(event.scene_id)
        if card is not None:
            card.set_state(self.engine.get_scene_state(event.scene_id))

    def _prepare_scene(self, scene: SceneDefinition) -> None:
        if scene.id in self._scene_tasks:
            self.status_message.emit(f"{scene.name} 已有场景任务在运行")
            return
        state = self.engine.get_scene_state(scene.id)
        key = f"agent:page:prepare:{scene.id}"
        self._scene_tasks[scene.id] = key
        self._cards[scene.id].set_busy(True)
        task = self._submit_task(
            key, _prepare_worker, self.engine, scene.id,
            state.model or scene.model, state.workspace_root,
            on_result=lambda result, scene_id=scene.id: self._scene_result(scene_id, result),
            on_error=lambda error, scene_id=scene.id: self._scene_error(scene_id, error),
            on_finished=lambda scene_id=scene.id: self._scene_finished(scene_id),
        )
        if task is None:
            self._scene_finished(scene.id)

    def _load_scene_model(self, scene: SceneDefinition) -> None:
        state = self.engine.get_scene_state(scene.id)
        model = state.model or scene.model
        if not model:
            self.status_message.emit("装入前必须明确填写模型")
            return
        answer = QMessageBox.question(
            self,
            "确认装入本机模型",
            f"将让本机已有的模型服务装入 {model}。不会启动任何服务，也不会下载模型。\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes or scene.id in self._scene_tasks:
            return
        key = f"agent:page:load:{scene.id}"
        self._scene_tasks[scene.id] = key
        self._cards[scene.id].set_busy(True)
        task = self._submit_task(
            key, _load_worker, self.engine, scene.id, model,
            on_result=lambda result, scene_id=scene.id: self._scene_result(scene_id, result),
            on_error=lambda error, scene_id=scene.id: self._scene_error(scene_id, error),
            on_finished=lambda scene_id=scene.id: self._scene_finished(scene_id),
        )
        if task is None:
            self._scene_finished(scene.id)

    def _scene_result(self, scene_id: str, state: SceneState) -> None:
        card = self._cards.get(scene_id)
        if card is None or not isinstance(state, SceneState):
            return
        card.set_state(state)
        self.status_message.emit(state.error or state.message or f"{card.scene.name} 状态已更新")

    def _scene_error(self, scene_id: str, error: Exception) -> None:
        card = self._cards.get(scene_id)
        message = str(error) or error.__class__.__name__
        if card is not None:
            card.show_task_error(message)
        self.status_message.emit(f"场景任务失败：{message}")

    def _scene_finished(self, scene_id: str) -> None:
        self._scene_tasks.pop(scene_id, None)
        card = self._cards.get(scene_id)
        if card is not None:
            card.set_busy(False)

    def _open_scene(self, scene: SceneDefinition) -> None:
        self.open_scene(scene.id)

    def open_scene(self, scene_id: str) -> AgentChatWindow | None:
        if self._closing:
            return None
        existing = self._windows.get(scene_id)
        if existing is not None:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return existing
        scene = self.engine.scenes.get(scene_id)
        window_class: type[AgentChatWindow]
        if scene.entry_point == EntryPoint.WORKSPACE:
            window_class = AgentWorkspaceWindow
        elif scene.entry_point == EntryPoint.COMPANION_SOFTWARE:
            window_class = AgentCompanionWindow
        else:
            window_class = AgentChatWindow
        window = window_class(self.engine, self.tasks, scene, parent=self)
        window.status_message.connect(self.status_message)
        window.navigation_requested.connect(self.navigation_requested)
        self._windows[scene_id] = window

        def removed(*_args: Any, key: str = scene_id, item: AgentChatWindow = window) -> None:
            if self._windows.get(key) is item:
                self._windows.pop(key, None)

        window.destroyed.connect(removed)
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def shutdown(self) -> None:
        if self._closing:
            return
        for window in tuple(self._windows.values()):
            window.close()
        self._windows.clear()
        self._shutdown_agent_owner()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)


class AgentChatWindow(_AgentTaskOwner, QMainWindow):
    status_message = Signal(str)
    navigation_requested = Signal(str)

    def __init__(
        self,
        engine: AgentEngine,
        tasks: TaskManager,
        scene: SceneDefinition,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.scene = scene
        self.session: Any = None
        self._stream_key = ""
        self._stream_session_id = ""
        self._streaming_message_id = ""
        self._last_assistant_id = ""
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(f"{scene.name} - AI 工作窗")
        self.resize(860, 680)
        self.setMinimumSize(620, 460)
        self._init_agent_owner(engine, tasks, self._on_engine_event)
        self._build_ui()
        self._create_session(initial=True)

    def _build_ui(self) -> None:
        central = QWidget()
        self.root_layout = QVBoxLayout(central)
        self.root_layout.setContentsMargins(14, 12, 14, 12)
        self.setCentralWidget(central)
        title_row = QHBoxLayout()
        title = QLabel(self.scene.name)
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        title_row.addWidget(title, 1)
        self.state_label = QLabel("未检查")
        self.state_label.setStyleSheet("color: #8dbdff; font-weight: 600;")
        title_row.addWidget(self.state_label)
        self.root_layout.addLayout(title_row)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("本机模型"))
        self.model_edit = QLineEdit(self.scene.model)
        self.model_edit.setPlaceholderText("填写已安装并装入的 model:tag；不会自动下载或装入")
        model_row.addWidget(self.model_edit, 1)
        self.check_button = QPushButton("检查状态")
        self.check_button.clicked.connect(self.prepare_scene)
        model_row.addWidget(self.check_button)
        self.new_button = QPushButton("新建会话")
        self.new_button.clicked.connect(self.new_session)
        model_row.addWidget(self.new_button)
        self.root_layout.addLayout(model_row)
        nav_row = QHBoxLayout()
        self.provider_label = QLabel("只连接本机已有的模型服务；不会自动启动或安装任何后端。")
        self.provider_label.setWordWrap(True)
        nav_row.addWidget(self.provider_label, 1)
        models_button = QPushButton("模型管理")
        models_button.clicked.connect(lambda: self.navigation_requested.emit("models"))
        downloads_button = QPushButton("资源下载")
        downloads_button.clicked.connect(lambda: self.navigation_requested.emit("downloads"))
        nav_row.addWidget(models_button)
        nav_row.addWidget(downloads_button)
        self.root_layout.addLayout(nav_row)
        self.status_label = QLabel("会话已创建；发送消息时才检查模型服务并请求真实回复。")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.root_layout.addWidget(self.status_label)
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("真实会话内容将显示在这里；不会生成模拟回复。")
        self.root_layout.addWidget(self._make_body(), 1)
        self.input_edit = QPlainTextEdit()
        self.input_edit.setPlaceholderText("输入消息……")
        self.input_edit.setMaximumHeight(100)
        self.root_layout.addWidget(self.input_edit)
        action_row = QHBoxLayout()
        self.session_label = QLabel()
        self.session_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        action_row.addWidget(self.session_label, 1)
        self.cancel_button = QPushButton("取消回复")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_stream)
        action_row.addWidget(self.cancel_button)
        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self.send_message)
        action_row.addWidget(self.send_button)
        self.root_layout.addLayout(action_row)

    def _make_body(self) -> QWidget:
        return self.transcript

    def _session_workspace_root(self) -> str:
        return ""

    def _session_changed(self) -> None:
        pass

    def _stream_state_changed(self, streaming: bool) -> None:
        pass

    def _prompt_for_engine(self, text: str) -> str:
        return text

    def _create_session(self, *, initial: bool = False) -> None:
        self.session = self.engine.create_session(
            self.scene.id,
            model=self.scene.model if initial else self.model_edit.text().strip(),
            workspace_root=self._session_workspace_root() or None,
        )
        self.model_edit.setText(self.session.model)
        self.input_edit.clear()
        self._reload_session()

    def _reload_session(self) -> None:
        self.session = self.engine.get_session(self.session.id)
        self.transcript.clear()
        self._streaming_message_id = ""
        self._last_assistant_id = ""
        for message in self.session.messages:
            role = "你" if message.role == "user" else "助手"
            suffix = "" if message.status == "completed" else f"\n[状态：{message.status}]"
            error = f"\n[真实错误：{message.error}]" if message.error else ""
            self.transcript.appendPlainText(f"{role}：\n{message.content}{suffix}{error}\n")
            if message.role == "assistant" and message.status == "completed":
                self._last_assistant_id = message.id
        self.session_label.setText(f"会话：{self.session.id}")
        self._session_changed()

    def new_session(self) -> None:
        if self._active_task_keys:
            self.status_message.emit("请先等待后台任务结束或取消回复，再新建会话")
            return
        self._create_session()
        self.status_label.setText("已通过现有 Session API 新建会话；旧会话仍保留在会话仓储中。")

    def _sync_model(self) -> None:
        selected = self.model_edit.text().strip()
        if selected == self.session.model:
            return
        self.session = self.engine.get_session(self.session.id)
        self.session.model = selected
        self.engine.sessions.save(self.session)

    def prepare_scene(self) -> None:
        if self._stream_key:
            self.status_message.emit("回复进行中，暂不重复检查场景")
            return
        self._sync_model()
        key = f"agent:window:prepare:{self.session.id}"
        self.check_button.setEnabled(False)
        task = self._submit_task(
            key, _prepare_worker, self.engine, self.scene.id,
            self.session.model, self._session_workspace_root(),
            on_result=self._show_state,
            on_error=self._prepare_error,
            on_finished=lambda: self.check_button.setEnabled(not self._stream_key),
        )
        if task is None:
            self.check_button.setEnabled(True)

    def _prepare_error(self, error: Exception) -> None:
        self._set_status(f"场景检查失败：{error}", error=True)

    def _on_engine_event(self, event: AgentEvent) -> None:
        if event.type == AgentEventType.STATE and event.scene_id == self.scene.id:
            self._show_state(self.engine.get_scene_state(self.scene.id))

    def _show_state(self, state: SceneState) -> None:
        if not isinstance(state, SceneState):
            return
        phase = _phase(state.phase)
        self.state_label.setText(_PHASE_TEXT[phase])
        provider = state.data.get("active_provider") or state.provider.get("active_provider") or "ollama"
        message = state.error or state.message
        if provider:
            label = _provider_text(provider)
            message = f"{message}\n模型服务：{label}" if message else f"模型服务：{label}"
        self._set_status(
            message or "检查完成。",
            error=phase in {ScenePhase.ERROR, ScenePhase.PROVIDER_OFFLINE},
        )

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"color: {'#ff8585' if error else '#b9c5d6'};")
        self.status_message.emit(message)

    def send_message(self) -> None:
        text = self.input_edit.toPlainText().strip()
        if not text or self._stream_key:
            return
        self._sync_model()
        prompt = self._prompt_for_engine(text)
        key = f"agent:stream:{self.session.id}"
        self._stream_key = key
        self._stream_session_id = self.session.id
        self.input_edit.clear()
        self._set_streaming(True)
        task = self._submit_task(
            key, _stream_worker, self.engine, self.session.id, prompt,
            on_progress=self._on_stream_event,
            on_error=self._on_stream_error,
            on_cancelled=self._on_stream_cancelled,
            on_finished=self._stream_finished,
        )
        if task is None:
            self._stream_finished()

    def _set_streaming(self, streaming: bool) -> None:
        self.send_button.setEnabled(not streaming)
        self.cancel_button.setEnabled(streaming)
        self.new_button.setEnabled(not streaming)
        self.check_button.setEnabled(not streaming)
        self.model_edit.setEnabled(not streaming)
        self._stream_state_changed(streaming)

    def cancel_stream(self) -> None:
        if not self._stream_key:
            return
        self._cancel_task(self._stream_key)
        self.engine.cancel(self._stream_session_id)
        self._set_status("已请求取消；等待模型服务确认，保留已经收到的真实内容。")

    def _on_stream_event(self, event: AgentEvent) -> None:
        if event.session_id != self.session.id:
            return
        if event.type == AgentEventType.USER_MESSAGE:
            message = event.data.get("message", {})
            self.transcript.appendPlainText(f"你：\n{message.get('content', '')}\n")
        elif event.type == AgentEventType.ASSISTANT_STARTED:
            self._streaming_message_id = event.message_id
            self.transcript.appendPlainText("助手：")
        elif event.type == AgentEventType.TOKEN:
            if event.message_id == self._streaming_message_id and event.delta:
                cursor = self.transcript.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(event.delta)
                self.transcript.setTextCursor(cursor)
                self.transcript.ensureCursorVisible()
        elif event.type == AgentEventType.ASSISTANT_COMPLETED:
            self._last_assistant_id = event.message_id
            self.transcript.appendPlainText("")
            self._set_status("内置 Agent 流式回复已完成。")
        elif event.type == AgentEventType.CANCELLED:
            self.transcript.appendPlainText("\n[回复已取消；以上仅为真实收到的部分内容。]")
            self._set_status("回复已取消，未将部分内容标记为成功。")
        elif event.type == AgentEventType.ERROR:
            message = event.error or "未知推理错误"
            self.transcript.appendPlainText(f"\n[真实错误：{message}]")
            self._set_status(f"回复失败：{message}", error=True)

    def _on_stream_error(self, error: Exception) -> None:
        message = str(error) or error.__class__.__name__
        self.transcript.appendPlainText(f"\n[后台任务错误：{message}]")
        self._set_status(f"后台回复任务失败：{message}", error=True)

    def _on_stream_cancelled(self) -> None:
        self._set_status("后台回复任务已确认取消；未伪造完成状态。")

    def _stream_finished(self) -> None:
        self._stream_key = ""
        self._stream_session_id = ""
        self._streaming_message_id = ""
        self._set_streaming(False)

    def shutdown(self) -> None:
        if self._closing:
            return
        if self._stream_key:
            self.engine.cancel(self._stream_session_id)
        self._shutdown_agent_owner()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)


class AgentWorkspaceWindow(AgentChatWindow):
    """AI 编程/工程维护共用的内置安全文件与 diff 工作窗。"""

    def __init__(
        self,
        engine: AgentEngine,
        tasks: TaskManager,
        scene: SceneDefinition,
        parent: QWidget | None = None,
    ):
        self.workspace_root = ""
        self.current_file: FileContent | None = None
        self._change_set: ChangeSet | None = None
        self._changes: dict[str, PendingChange] = {}
        super().__init__(engine, tasks, scene, parent)
        self.setWindowTitle(f"{scene.name} - AI 工作窗")
        self.resize(1180, 780)

    def _make_body(self) -> QWidget:
        file_panel = QGroupBox("工作区检索与阅读")
        file_layout = QVBoxLayout(file_panel)
        choose_row = QHBoxLayout()
        self.choose_button = QPushButton("选择工作区")
        self.choose_button.clicked.connect(self.choose_workspace)
        choose_row.addWidget(self.choose_button)
        self.terminal_button = QPushButton("生成终端计划")
        self.terminal_button.setVisible(self.scene.kind == SceneKind.MAINTENANCE)
        self.terminal_button.clicked.connect(self.create_terminal_plan)
        choose_row.addWidget(self.terminal_button)
        file_layout.addLayout(choose_row)
        self.root_label = QLabel("尚未选择；检索、读取和写入均不可用。")
        self.root_label.setWordWrap(True)
        self.root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        file_layout.addWidget(self.root_label)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("文件名/相对路径，或勾选后搜索内容")
        self.search_edit.returnPressed.connect(self.search_files)
        file_layout.addWidget(self.search_edit)
        search_row = QHBoxLayout()
        self.content_check = QCheckBox("搜索内容")
        search_row.addWidget(self.content_check)
        self.pattern_edit = QLineEdit("*")
        self.pattern_edit.setPlaceholderText("文件模式，例如 *.py")
        search_row.addWidget(self.pattern_edit, 1)
        self.search_button = QPushButton("检索")
        self.search_button.clicked.connect(self.search_files)
        search_row.addWidget(self.search_button)
        file_layout.addLayout(search_row)
        self.results_list = QListWidget()
        self.results_list.itemDoubleClicked.connect(lambda _item: self.read_selected_file())
        file_layout.addWidget(self.results_list, 1)
        self.read_button = QPushButton("读取选中文件")
        self.read_button.clicked.connect(self.read_selected_file)
        file_layout.addWidget(self.read_button)
        self.file_label = QLabel("未读取文件")
        self.file_label.setWordWrap(True)
        file_layout.addWidget(self.file_label)
        self.file_view = QPlainTextEdit()
        self.file_view.setReadOnly(True)
        self.file_view.setPlaceholderText("通过现有安全文件 API 读取的 UTF-8 文本")
        file_layout.addWidget(self.file_view, 2)
        self.include_file_check = QCheckBox("发送时附带当前文件真实内容")
        self.include_file_check.setChecked(True)
        file_layout.addWidget(self.include_file_check)

        chat_panel = QGroupBox("内置 Agent 对话（不会自行写文件）")
        chat_layout = QVBoxLayout(chat_panel)
        chat_layout.addWidget(self.transcript)
        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        top_splitter.addWidget(file_panel)
        top_splitter.addWidget(chat_panel)
        top_splitter.setSizes([520, 560])

        diff_panel = QGroupBox("ChangeSuggestion / 逐文件 diff 预览与明确确认")
        diff_layout = QVBoxLayout(diff_panel)
        diff_buttons = QHBoxLayout()
        self.parse_button = QPushButton("解析上一条完成回复")
        self.parse_button.setEnabled(False)
        self.parse_button.clicked.connect(self.preview_last_response)
        diff_buttons.addWidget(self.parse_button)
        self.apply_button = QPushButton("确认写入当前文件")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_selected_change)
        diff_buttons.addWidget(self.apply_button)
        self.change_status_label = QLabel("修改会先显示差异，只有你确认后才写入文件。")
        self.change_status_label.setWordWrap(True)
        diff_buttons.addWidget(self.change_status_label, 1)
        diff_layout.addLayout(diff_buttons)
        diff_body = QSplitter(Qt.Orientation.Horizontal)
        self.changes_list = QListWidget()
        self.changes_list.currentItemChanged.connect(self._show_selected_change)
        diff_body.addWidget(self.changes_list)
        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setPlaceholderText("选择左侧文件查看后端生成的真实 unified diff 与指纹。")
        diff_body.addWidget(self.diff_view)
        diff_body.setSizes([300, 760])
        diff_layout.addWidget(diff_body)

        outer = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(top_splitter)
        outer.addWidget(diff_panel)
        outer.setSizes([450, 270])
        return outer

    def _session_workspace_root(self) -> str:
        return getattr(self, "workspace_root", "")

    def _session_changed(self) -> None:
        self.workspace_root = self.session.workspace_root
        self.root_label.setText(self.workspace_root or "尚未选择；检索、读取和写入均不可用。")
        self.results_list.clear()
        self.file_view.clear()
        self.file_label.setText("未读取文件")
        self.current_file = None
        self._clear_changes()
        self.parse_button.setEnabled(bool(self._last_assistant_id) and not self._stream_key)

    def _stream_state_changed(self, streaming: bool) -> None:
        if hasattr(self, "parse_button"):
            self.parse_button.setEnabled(bool(self._last_assistant_id) and not streaming)
        if hasattr(self, "choose_button"):
            self.choose_button.setEnabled(not streaming)

    def choose_workspace(self) -> None:
        if self._active_task_keys:
            self.status_message.emit("后台任务进行中，暂不能切换工作区")
            return
        selected = QFileDialog.getExistingDirectory(
            self, "选择具体项目工作区", self.workspace_root,
        )
        if not selected:
            return
        try:
            root = self.engine.select_workspace(selected, session_id=self.session.id)
        except Exception as exc:
            QMessageBox.warning(self, "工作区不可用", str(exc))
            return
        self.workspace_root = root
        self.session = self.engine.get_session(self.session.id)
        self.root_label.setText(root)
        self.results_list.clear()
        self.file_view.clear()
        self.current_file = None
        self._clear_changes()
        self._set_status(f"已显式选择工作区：{root}；尚未读取或写入文件。")

    def search_files(self) -> None:
        if not self.workspace_root:
            self._set_status("请先选择工作区。", error=True)
            return
        key = f"agent:search:{self.session.id}"
        self.search_button.setEnabled(False)
        task = self._submit_task(
            key, _search_worker, self.engine, self.session.id,
            self.search_edit.text().strip(), self.content_check.isChecked(),
            self.pattern_edit.text().strip() or "*",
            on_result=self._show_search_result,
            on_error=lambda error: self._set_status(f"检索失败：{error}", error=True),
            on_finished=lambda: self.search_button.setEnabled(True),
        )
        if task is None:
            self.search_button.setEnabled(True)

    def _show_search_result(self, result: SearchResult) -> None:
        if not isinstance(result, SearchResult):
            return
        self.results_list.clear()
        for match in result.matches:
            location = f":{match.line}:{match.column}" if match.line else ""
            text = f"{match.path}{location}  ({match.size_bytes} 字节)"
            if match.preview:
                text += f"\n{match.preview}"
            item = QListWidgetItem(text)
            item.setData(_ITEM_ROLE, match.path)
            self.results_list.addItem(item)
        notes = list(result.issues)
        if result.truncated:
            notes.append("结果已按后端安全上限截断")
        self._set_status(f"检索到 {len(result.matches)} 个文件。" + ("\n" + "\n".join(notes) if notes else ""))

    def read_selected_file(self) -> None:
        item = self.results_list.currentItem()
        if item is None:
            self._set_status("请先在检索结果中选择文件。")
            return
        path = str(item.data(_ITEM_ROLE) or "")
        key = f"agent:read:{self.session.id}"
        self.read_button.setEnabled(False)
        task = self._submit_task(
            key, _read_worker, self.engine, self.session.id, path,
            on_result=self._show_file,
            on_error=lambda error: self._set_status(f"读取失败：{error}", error=True),
            on_finished=lambda: self.read_button.setEnabled(True),
        )
        if task is None:
            self.read_button.setEnabled(True)

    def _show_file(self, result: FileContent) -> None:
        if not isinstance(result, FileContent):
            return
        self.current_file = result
        self.file_label.setText(f"{result.path}｜{result.size_bytes} 字节｜SHA-256 {result.sha256}")
        self.file_view.setPlainText(result.content)
        self._set_status(f"已读取真实文件：{result.path}；尚未生成任何写入改动。")

    def _prompt_for_engine(self, text: str) -> str:
        current = self.current_file
        if current is None or not self.include_file_check.isChecked():
            return text
        return (
            "以下是宿主通过安全文件 API 读取的真实工作区文件，不得声称读取了其他文件：\n"
            f"路径：{current.path}\nSHA-256：{current.sha256}\n"
            f"```text\n{current.content}\n```\n\n用户请求：\n{text}"
        )

    def preview_last_response(self) -> None:
        if not self.workspace_root:
            self._set_status("请先选择工作区再解析改动。", error=True)
            return
        if not self._last_assistant_id or self._stream_key:
            self._set_status("没有可解析的已完成助手回复。")
            return
        key = f"agent:preview:{self.session.id}"
        self.parse_button.setEnabled(False)
        task = self._submit_task(
            key, _preview_worker, self.engine, self.session.id, self._last_assistant_id,
            on_result=self._show_preview_result,
            on_error=lambda error: self._set_status(f"改动解析失败：{error}", error=True),
            on_finished=lambda: self.parse_button.setEnabled(bool(self._last_assistant_id)),
        )
        if task is None:
            self.parse_button.setEnabled(True)

    def _show_preview_result(self, result: tuple[ChangeSet, SuggestionParseResult]) -> None:
        if not isinstance(result, tuple) or len(result) != 2:
            return
        change_set, parsed = result
        self._change_set = change_set
        self._changes = {item.id: item for item in change_set.changes}
        self.changes_list.clear()
        for change in change_set.changes:
            item = QListWidgetItem(self._change_text(change))
            item.setData(_ITEM_ROLE, change.id)
            self.changes_list.addItem(item)
        issues = list(dict.fromkeys([*change_set.issues, *parsed.issues]))
        if change_set.changes:
            self.changes_list.setCurrentRow(0)
            self.change_status_label.setText(
                f"已生成 {len(change_set.changes)} 处待确认修改，尚未写入。"
                + ("\n" + "\n".join(issues) if issues else "")
            )
        else:
            self.diff_view.setPlainText("\n".join(issues) or "回复中没有有效 ChangeSuggestion，未生成写入计划。")
            self.change_status_label.setText("没有可写入的改动。")
            self.apply_button.setEnabled(False)

    @staticmethod
    def _change_text(change: PendingChange) -> str:
        return f"{change.path}｜{change.operation.value}｜{change.status}"

    def _selected_change(self) -> PendingChange | None:
        item = self.changes_list.currentItem()
        if item is None:
            return None
        return self._changes.get(str(item.data(_ITEM_ROLE) or ""))

    def _show_selected_change(self, *_args: Any) -> None:
        change = self._selected_change()
        if change is None:
            self.diff_view.clear()
            self.apply_button.setEnabled(False)
            return
        self.diff_view.setPlainText(
            f"文件：{change.path}\n操作：{change.operation.value}\n状态：{change.status}\n"
            f"工作区：{change.workspace_root}\n原始 SHA-256：{change.original_sha256 or '(新增文件)'}\n"
            f"拟写入 SHA-256：{change.proposed_sha256 or '(删除文件)'}\n\n{change.diff}"
        )
        self.apply_button.setEnabled(change.status == "pending")

    def _clear_changes(self) -> None:
        self._change_set = None
        self._changes = {}
        if hasattr(self, "changes_list"):
            self.changes_list.clear()
            self.diff_view.clear()
            self.apply_button.setEnabled(False)
            self.change_status_label.setText("修改会先显示差异，只有你确认后才写入文件。")

    def apply_selected_change(self) -> None:
        change = self._selected_change()
        if change is None or change.status != "pending":
            return
        answer = QMessageBox.question(
            self,
            "明确确认写入当前文件",
            f"工作区：{change.workspace_root}\n文件：{change.path}\n操作：{change.operation.value}\n\n"
            "只有本次明确确认才会通过 request_change_confirmation 获取一次性令牌，"
            "并写入这个文件。其他文件不会被改动。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.change_status_label.setText("已取消，未写入任何文件。")
            return
        key = f"agent:apply:{self.session.id}:{change.id}"
        self.apply_button.setEnabled(False)
        task = self._submit_task(
            key, _apply_worker, self.engine, self.session.id, change.id,
            on_result=lambda result, change_id=change.id: self._show_apply_result(change_id, result),
            on_error=lambda error: self._set_status(f"写入任务失败：{error}", error=True),
            on_finished=self._show_selected_change,
        )
        if task is None:
            self.apply_button.setEnabled(True)

    def _show_apply_result(self, change_id: str, result: ApplyResult) -> None:
        if not isinstance(result, ApplyResult):
            return
        change = self._changes.get(change_id)
        if change is not None:
            change.status = result.status
            for row in range(self.changes_list.count()):
                item = self.changes_list.item(row)
                if item.data(_ITEM_ROLE) == change_id:
                    item.setText(self._change_text(change))
                    break
        self.change_status_label.setText(result.message)
        self._set_status(result.message, error=not result.success)
        self._show_selected_change()

    def create_terminal_plan(self) -> None:
        if not self.workspace_root:
            self._set_status("请先选择工作区再生成终端计划。", error=True)
            return
        try:
            plan = self.engine.commands.create_terminal_plan(self.workspace_root)
        except Exception as exc:
            self._set_status(f"终端计划生成失败：{exc}", error=True)
            return
        _show_command_plans(self, [plan])


class AgentCompanionWindow(AgentChatWindow):
    """视频策划对话与配套软件计划；不提供伪造的视频处理结果。"""

    def _make_body(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.transcript, 1)
        companion = QGroupBox("官方/本地配套软件启动计划（当前未启动）")
        companion_layout = QVBoxLayout(companion)
        self.companion_status = QLabel(
            "只使用场景与设置中已验证的本地程序。生成计划前会检查真实状态；不下载、不安装、不执行。"
        )
        self.companion_status.setWordWrap(True)
        companion_layout.addWidget(self.companion_status)
        self.plan_button = QPushButton("确认检查并生成配套软件计划")
        self.plan_button.clicked.connect(self.create_companion_plans)
        companion_layout.addWidget(self.plan_button)
        layout.addWidget(companion)
        return container

    def _show_state(self, state: SceneState) -> None:
        super()._show_state(state)
        if not hasattr(self, "companion_status") or not isinstance(state, SceneState):
            return
        if state.software:
            lines = [
                f"{item.name}：{'已验证' if item.installed else '未配置/不可用'}；"
                f"{item.executable or item.message}"
                for item in state.software
            ]
            self.companion_status.setText("\n".join(lines))

    def create_companion_plans(self) -> None:
        answer = QMessageBox.question(
            self,
            "确认生成配套软件计划",
            "将检查本机模型服务和配套软件的真实状态，并只列出准备步骤；"
            "不会启动任何程序、下载软件或执行步骤。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._sync_model()
        key = f"agent:companion-plan:{self.session.id}"
        self.plan_button.setEnabled(False)
        task = self._submit_task(
            key, _companion_plan_worker, self.engine, self.scene.id, self.session.model,
            on_result=self._show_companion_plans,
            on_error=lambda error: self._set_status(f"配套软件计划未生成：{error}", error=True),
            on_finished=lambda: self.plan_button.setEnabled(True),
        )
        if task is None:
            self.plan_button.setEnabled(True)

    def _show_companion_plans(self, plans: list[CommandPlan]) -> None:
        if not isinstance(plans, list) or not all(isinstance(item, CommandPlan) for item in plans):
            self._set_status("后端返回了无法识别的配套软件计划。", error=True)
            return
        self.companion_status.setText(f"已生成 {len(plans)} 个计划；尚未启动任何程序。")
        _show_command_plans(self, plans)


def _show_command_plans(parent: QWidget, plans: list[CommandPlan]) -> None:
    if not plans:
        QMessageBox.information(parent, "准备步骤", "没有需要执行的步骤；未启动任何程序。")
        return
    details: list[str] = []
    commands: list[str] = []
    for plan in plans:
        command = plan.command_text
        commands.append(command)
        details.append(
            f"标题：{plan.title}\n程序：{plan.program}\n逐参数：{list(plan.args)!r}\n"
            f"命令：{command}\n工作目录：{plan.cwd or '(未指定)'}\n用途：{plan.purpose}\n"
            f"影响：{plan.impact}\n风险：{plan.risk}\n类型：{plan.kind}\n"
            f"允许核心执行：{plan.allow_execute}\n计划 ID：{plan.id}\n"
            "状态：仅展示/复制，界面未请求执行确认，未启动程序。"
        )
    dialog = QMessageBox(parent)
    dialog.setWindowTitle("准备步骤（未执行）")
    dialog.setIcon(QMessageBox.Icon.Information)
    dialog.setText(
        f"已生成 {len(plans)} 个真实 CommandPlan。此窗口只展示和复制，不调用 execute_command_plan。"
    )
    dialog.setDetailedText("\n\n".join(details))
    copy_button = dialog.addButton("复制全部命令", QMessageBox.ButtonRole.ActionRole)
    dialog.addButton(QMessageBox.StandardButton.Close)
    dialog.exec()
    if dialog.clickedButton() is copy_button:
        QGuiApplication.clipboard().setText("\n".join(commands))
