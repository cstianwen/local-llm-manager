"""模型管理首页：SQLite 列表、真实后台任务、受控生命周期与 T4 区域。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core.hardware import HardwareCollector, HardwareSnapshot
from ..core.models import ModelInfo, OperationResult, RunningModel, human_size
from ..core.phases import phase_text
from ..core.preflight import PreflightResult
from ..core.settings import Settings
from ..core.tasks import Task, TaskManager
from ..providers.base import ProviderStatus
from ..providers.registry import ProviderRegistry
from ..providers.t4 import T4Provider
from ..services.model_service import ModelService, ReleaseEvidence
from ..store.db import ModelStore
from .dispatch import GuiDispatch
from .scan_scope_dialog import ScanScopeDialog
from .t4_panel import T4Panel


_MODEL_ROLE = int(Qt.ItemDataRole.UserRole)
_LOADED_COLOR = QColor("#63d68b")
_CALLER_COLOR = QColor("#ff7373")
_MUTED_COLOR = QColor("#93a1b5")


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {}


def _check_token(token: Any) -> None:
    if token is not None:
        token.raise_if_cancelled()


def _status_worker(token: Any, progress: Callable[[Any], None], service: ModelService) -> dict[str, Any]:
    """状态之外补充每个已装入模型的只读占用证据，不终止进程。"""
    result = service.refresh_status(token=token, progress=progress)
    models = service.store.all()
    evidence: dict[str, dict[str, Any]] = {}
    for running in result["running"]:
        _check_token(token)
        for model in models:
            if not _running_matches(model, running):
                continue
            try:
                inspected = service.inspect_release(model.file_path)
                evidence[model.model_id] = inspected.to_dict()
            except Exception as exc:
                evidence[model.model_id] = {"model_id": model.model_id, "error": str(exc), "inspections": []}
    result["evidence"] = evidence
    return result


def _hardware_worker(token: Any, progress: Callable[[Any], None], store: ModelStore) -> HardwareSnapshot:
    progress({"message": "正在采集 CPU、内存、GPU 与既有 Ollama 状态。"})
    snapshot = HardwareCollector(include_ollama=True).collect()
    _check_token(token)
    store.hardware.save(snapshot.to_dict(), snapshot.captured_at)
    progress({"message": "硬件快照已写入 SQLite 缓存。"})
    return snapshot


def _preflight_worker(
    token: Any,
    progress: Callable[[Any], None],
    service: ModelService,
    reference: str,
    device: str,
    backend: str,
    selected_gpu: dict[str, Any] | None = None,
    context_length: int | None = None,
) -> dict[str, Any]:
    progress({"message": "正在采集硬件并执行真实容量/后端预检。"})
    snapshot = HardwareCollector(include_ollama=True).collect()
    _check_token(token)
    service.store.hardware.save(snapshot.to_dict(), snapshot.captured_at)
    if selected_gpu is not None:
        position = selected_gpu.get("index")
        expected_name = str(selected_gpu.get("name") or "")
        if not isinstance(position, int) or not 0 <= position < len(snapshot.gpus):
            raise ValueError("所选 Ollama 显卡已不在新鲜硬件快照的同一索引中，请重新扫描本机配置。")
        gpu = snapshot.gpus[position]
        if expected_name and gpu.name != expected_name:
            raise ValueError(
                f"GPU{position} 已从 {expected_name} 变为 {gpu.name}，为避免选错卡已停止预检；请重新扫描本机配置。"
            )
        snapshot.gpus = [gpu]
    check = service.preflight(
        reference, device=device, backend=backend, hardware=snapshot,
        context_length=context_length,
    )
    return {"check": check, "hardware": snapshot, "selected_gpu": selected_gpu}


def _inspect_worker(token: Any, progress: Callable[[Any], None], service: ModelService, reference: str) -> ReleaseEvidence:
    progress({"message": "正在收集文件、端口、日志和受管进程证据。"})
    result = service.inspect_release(reference)
    _check_token(token)
    return result


def _release_worker(
    token: Any,
    progress: Callable[[Any], None],
    service: ModelService,
    reference: str,
    process: str | None,
) -> OperationResult:
    _check_token(token)
    progress({"message": "证据已确认，正在执行安全释放。"})
    result = service.release_model(reference, confirmation=True, process=process)
    _check_token(token)
    return result


def _delete_worker(token: Any, progress: Callable[[Any], None], service: ModelService, model: ModelInfo) -> OperationResult:
    _check_token(token)
    progress({"message": "正在按模型来源执行隔离或 Provider API 删除。"})
    result = service.delete_model(
        model.file_path,
        confirmation=True,
        path=model.file_path if model.provider == "local-file" else None,
    )
    _check_token(token)
    return result


def _running_matches(model: ModelInfo, running: RunningModel) -> bool:
    if model.provider == "local-file":
        return running.provider != "ollama" and running.name == model.file_path
    return running.provider == model.provider and running.name in {
        model.model_ref, model.file_name, model.display_name,
    }


def _callers(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    found: dict[tuple[Any, ...], dict[str, Any]] = {}
    for inspection in evidence.get("inspections", []):
        result = inspection.get("result", {})
        for user in result.get("users", []):
            key = (user.get("pid"), user.get("name"), user.get("target_port"), user.get("provider"))
            found[key] = user
    return list(found.values())


def _evidence_text(value: ReleaseEvidence | dict[str, Any]) -> str:
    evidence = _dict(value)
    lines = [f"模型：{evidence.get('model_id', '')}"]
    if evidence.get("error"):
        lines.append(f"证据收集错误：{evidence['error']}")
    running = evidence.get("running_models", [])
    lines.append("\n[后端运行记录]")
    if running:
        for item in running:
            lines.append(
                f"- {item.get('provider', '')} / {item.get('name', '')}；"
                f"内存 {human_size(item.get('size_bytes', 0))}；显存 {human_size(item.get('size_vram', 0))}；"
                f"过期时间 {item.get('expires_at') or '未报告'}"
            )
    else:
        lines.append("- 本次未获得匹配的后端运行记录。")
    processes = evidence.get("managed_processes", [])
    lines.append("\n[本工具创建的受管进程]")
    if processes:
        for process in processes:
            lines.append(
                f"- id={process.get('id')} PID={process.get('pid')} "
                f"provider={process.get('provider')} status={process.get('status')}"
            )
    else:
        lines.append("- 无。不会根据外部 PID 猜测或终止进程。")
    lines.append("\n[Inspector 证据与调用方]")
    inspections = evidence.get("inspections", [])
    if not inspections:
        lines.append("- 无占用检查记录。")
    for inspection in inspections:
        result = inspection.get("result", {})
        lines.append(
            f"\n类型={inspection.get('kind', '')} 目标={inspection.get('path') or result.get('target') or inspection.get('port') or ''}"
        )
        if inspection.get("error"):
            lines.append(f"错误：{inspection['error']}")
        confidence = result.get("confidence", "unknown")
        lines.append(f"置信度：{getattr(confidence, 'value', confidence)}；{result.get('message', '')}")
        users = result.get("users", [])
        if users:
            for user in users:
                lines.append(
                    f"  调用/占用进程 PID={user.get('pid')} name={user.get('name') or '未知'} "
                    f"provider={user.get('provider') or ''} port={user.get('target_port') or ''}\n"
                    f"  exe={user.get('exe_path') or '未知'}"
                )
        else:
            lines.append("  未报告调用/占用进程。")
        for fact in result.get("evidence", []):
            lines.append(f"  证据：{fact}")
        for error in result.get("errors", []):
            lines.append(f"  错误：{error}")
        if result.get("requires_elevation"):
            lines.append("  部分证据需要管理员权限。")
    lines.append("\n安全边界：释放只调用 Provider 卸载或停止明确选择的本工具受管进程，不杀非受管进程。")
    return "\n".join(lines)


class TextDialog(QDialog):
    def __init__(self, title: str, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(780, 560)
        layout = QVBoxLayout(self)
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(text)
        layout.addWidget(viewer)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class LoadModelDialog(QDialog):
    """配置设备、真实预检，并在结果展示后进行第二次明确确认。"""

    def __init__(
        self,
        model: ModelInfo,
        service: ModelService,
        tasks: TaskManager,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.model = model
        self.service = service
        self.tasks = tasks
        self.backend = "llamacpp" if model.provider == "local-file" else ""
        self.check: PreflightResult | None = None
        self.preflight_hardware: HardwareSnapshot | None = None
        self._preflight_version = 0
        self._dispatch = GuiDispatch(self)
        self._task_key = f"preflight-dialog:{id(self)}"
        self.setWindowTitle(f"装入模型 - {model.display_name or model.file_name}")
        self.resize(690, 570)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("不使用预设", None)
        rows = service.store.connection.execute(
            "SELECT * FROM load_presets WHERE model_id='' OR model_id=? ORDER BY name",
            (model.model_id,),
        ).fetchall()
        for row in rows:
            self.preset_combo.addItem(str(row["name"]), dict(row))
        form.addRow("装入参数预设", self.preset_combo)
        self.device_combo = QComboBox()
        self.device_combo.addItem("自动", "auto")
        self.device_combo.addItem("CPU", "cpu")
        self.device_combo.addItem("GPU", "gpu")
        form.addRow("计算设备", self.device_combo)
        self.context_spin = QSpinBox()
        self.context_spin.setRange(0, 1_048_576)
        self.context_spin.setSpecialValueText("模型默认")
        form.addRow("上下文长度", self.context_spin)
        self.gpu_layers_check = QCheckBox("显式指定 GPU 层数（仅本地 llama.cpp）")
        self.gpu_layers_spin = QSpinBox()
        self.gpu_layers_spin.setRange(-1, 4096)
        self.gpu_layers_spin.setValue(-1)
        self.gpu_layers_spin.setSpecialValueText("全部可卸载层")
        self.gpu_layers_spin.setEnabled(False)
        self.gpu_layers_check.setEnabled(model.provider == "local-file")
        layers_row = QHBoxLayout()
        layers_row.addWidget(self.gpu_layers_check)
        layers_row.addWidget(self.gpu_layers_spin)
        layers_row.addStretch(1)
        form.addRow("GPU 卸载", layers_row)
        self.ollama_gpu_combo = QComboBox()
        if model.provider == "ollama":
            cached = service.store.hardware.latest()
            data = cached.get("data", {}) if cached else {}
            gpus = data.get("gpus", []) if isinstance(data, dict) else []
            for index, gpu in enumerate(gpus):
                if not isinstance(gpu, dict):
                    continue
                name = str(gpu.get("name") or "未知显卡")
                free = gpu.get("free_vram_bytes")
                free_text = human_size(free) if free is not None else "空闲显存未知"
                self.ollama_gpu_combo.addItem(
                    f"GPU{index} · {name} · {free_text}",
                    {"index": index, "name": name},
                )
            if self.ollama_gpu_combo.count() == 0:
                self.ollama_gpu_combo.addItem("无缓存显卡；请先扫描本机配置", None)
            self.ollama_gpu_combo.setEnabled(False)
            form.addRow("Ollama 显卡", self.ollama_gpu_combo)
            gpu_hint = QLabel(
                "CPU 将传 num_gpu=0；GPU 将传所选缓存索引 main_gpu。"
                "预检会刷新硬件并核对同位置、同名称的单卡，不会改用最大卡。"
            )
            gpu_hint.setWordWrap(True)
            form.addRow("设备参数", gpu_hint)
        elif model.provider == "local-file":
            limitation = QLabel(
                "llama.cpp 当前只能传 GPU 层数，不能指定具体显卡；"
                "实际显卡由后端及其运行环境决定，本工具不会伪传 main_gpu。"
            )
            limitation.setWordWrap(True)
            limitation.setStyleSheet("color: #d8b56a;")
            form.addRow("显卡选择限制", limitation)
        backend_text = self.backend or model.provider
        form.addRow("实际后端", QLabel(backend_text))
        layout.addLayout(form)

        self.result_view = QPlainTextEdit()
        self.result_view.setReadOnly(True)
        self.result_view.setPlaceholderText("正在准备预检；预检不会装入模型。")
        layout.addWidget(self.result_view, 1)

        buttons = QHBoxLayout()
        self.preflight_button = QPushButton("重新预检")
        self.load_button = QPushButton("查看预检后确认装入")
        self.load_button.setEnabled(False)
        cancel_button = QPushButton("取消")
        buttons.addWidget(self.preflight_button)
        buttons.addStretch(1)
        buttons.addWidget(self.load_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

        self.preflight_button.clicked.connect(self.run_preflight)
        self.load_button.clicked.connect(self._confirm_load)
        cancel_button.clicked.connect(self.reject)
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self.context_spin.valueChanged.connect(self._invalidate)
        self.ollama_gpu_combo.currentIndexChanged.connect(self._invalidate)
        self.gpu_layers_check.toggled.connect(self.gpu_layers_spin.setEnabled)
        self.gpu_layers_check.toggled.connect(self._invalidate)
        self.gpu_layers_spin.valueChanged.connect(self._invalidate)
        QTimer.singleShot(0, self.run_preflight)

    @property
    def device(self) -> str:
        return str(self.device_combo.currentData())

    def _apply_preset(self, *_args: Any) -> None:
        preset = self.preset_combo.currentData()
        if not isinstance(preset, dict):
            return
        device = str(preset.get("device") or "auto")
        index = self.device_combo.findData(device)
        self.device_combo.setCurrentIndex(max(0, index))
        try:
            context = max(0, int(preset.get("context_length") or 0))
            layers = int(preset.get("gpu_layers") or 0)
        except (TypeError, ValueError, OverflowError):
            self._invalidate()
            return
        self.context_spin.setValue(context)
        if self.model.provider == "local-file":
            self.gpu_layers_spin.setValue(max(-1, min(4096, layers)))
            self.gpu_layers_check.setChecked(device != "auto" or layers != 0)
        self._invalidate()

    def _selected_gpu(self) -> dict[str, Any] | None:
        value = self.ollama_gpu_combo.currentData()
        return dict(value) if isinstance(value, dict) else None

    def load_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.model.provider == "local-file":
            if self.gpu_layers_check.isChecked():
                options["gpu_layers"] = self.gpu_layers_spin.value()
            elif self.device == "cpu":
                options["gpu_layers"] = 0
            elif self.device == "gpu":
                options["gpu_layers"] = -1
        elif self.model.provider == "ollama":
            if self.device == "cpu":
                options["num_gpu"] = 0
            elif self.device == "gpu":
                selected = self._selected_gpu()
                if selected is not None:
                    options["main_gpu"] = selected["index"]
        context_length = self.context_spin.value() or None
        if context_length is not None and self.model.provider == "ollama":
            options["num_ctx"] = context_length
        return {
            "device": self.device,
            "backend": self.backend,
            "context_length": context_length,
            "options": options,
            "hardware": self.preflight_hardware,
        }

    def _on_device_changed(self, *_args: Any) -> None:
        if self.model.provider == "ollama":
            has_gpu = self.ollama_gpu_combo.currentData() is not None
            self.ollama_gpu_combo.setEnabled(self.device == "gpu" and has_gpu)
        self._invalidate()

    def _invalidate(self, *_args: Any) -> None:
        self._preflight_version += 1
        self.check = None
        self.preflight_hardware = None
        self.load_button.setEnabled(False)
        self.result_view.setPlainText("设备、显卡或 GPU 层数已改变，请按新配置重新预检。")

    def run_preflight(self) -> None:
        self._preflight_version += 1
        version = self._preflight_version
        self.check = None
        self.preflight_hardware = None
        self.load_button.setEnabled(False)
        self.preflight_button.setEnabled(False)
        selected_gpu = (
            self._selected_gpu()
            if self.model.provider == "ollama" and self.device == "gpu"
            else None
        )
        if self.model.provider == "ollama" and self.device == "gpu" and selected_gpu is None:
            self.preflight_button.setEnabled(True)
            self.result_view.setPlainText(
                "无法执行 Ollama GPU 预检：硬件缓存中没有可选显卡。\n"
                "请先关闭本对话框，点击“扫描本机配置”，再重新打开并选择具体显卡。"
            )
            return
        self.result_view.setPlainText("正在采集硬件并调用 ModelService.preflight，尚未装入模型……")
        task = self.tasks.submit(
            self._task_key, _preflight_worker,
            self.service, self.model.file_path, self.device, self.backend, selected_gpu,
            self.context_spin.value() or None,
        )
        if task is None:
            self.result_view.setPlainText("相同预检正在执行，请稍候。")
            return
        def deliver(callback: Callable[..., None], *values: Any) -> None:
            def run() -> None:
                if version == self._preflight_version:
                    callback(*values)

            self._dispatch.post(run)

        task.signals.result.connect(lambda payload: deliver(self._on_preflight, payload))
        task.signals.error.connect(lambda error: deliver(self._on_preflight_error, error))
        task.signals.finished.connect(lambda: self._dispatch.post(
            lambda: self.preflight_button.setEnabled(True)
        ))

    def _on_preflight(self, payload: dict[str, Any]) -> None:
        self.check = payload["check"]
        self.preflight_hardware = payload["hardware"]
        check = self.check
        lines = [
            f"结论：{check.message}",
            f"状态：{check.status}；置信度：{check.confidence}；可尝试：{check.can_attempt}",
            f"请求设备：{check.device}；预检设备：{check.selected_device}",
            f"模型大小：{human_size(check.model_size_bytes)}",
            f"可用内存：{human_size(check.available_ram_bytes)}；估算内存需求：{human_size(check.estimated_ram_bytes)}",
            f"可用显存：{human_size(check.available_vram_bytes) if check.available_vram_bytes is not None else '未知'}；"
            f"估算显存需求：{human_size(check.estimated_vram_bytes)}",
            "预检不是装入成功保证。",
        ]
        for title, values in (
            ("错误", check.errors), ("警告", check.warnings),
            ("估算假设", check.assumptions), ("建议", check.recommendations),
        ):
            if values:
                lines.append(f"\n[{title}]")
                lines.extend(f"- {value}" for value in values)
        selected_gpu = payload.get("selected_gpu")
        if selected_gpu is not None:
            lines.insert(
                3,
                f"Ollama 单卡：GPU{selected_gpu['index']} {selected_gpu.get('name', '')}；"
                f"装入参数 main_gpu={selected_gpu['index']}",
            )
        self.result_view.setPlainText("\n".join(lines))
        self.load_button.setEnabled(check.can_attempt)
        self.load_button.setText("二次确认并装入" if check.can_attempt else "预检阻塞，不能装入")

    def _on_preflight_error(self, error: Exception) -> None:
        self.check = None
        self.preflight_hardware = None
        self.load_button.setEnabled(False)
        self.result_view.setPlainText(f"预检失败，未调用装入：\n{error}")

    def _confirm_load(self) -> None:
        if self.check is None or not self.check.can_attempt:
            return
        options = self.load_options()
        answer = QMessageBox.question(
            self,
            "二次确认装入",
            f"预检结果已展示（{self.check.status}）。现在提交真实装入任务？\n\n"
            f"模型：{self.model.display_name or self.model.file_name}\n"
            f"设备：{self.device}\nGPU 参数：{options['options'] or '由后端决定'}\n\n"
            "预检不保证成功，结果将以 Provider 返回为准。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.accept()

    def reject(self) -> None:
        self.tasks.cancel(self._task_key)
        super().reject()


class ReleaseDialog(QDialog):
    def __init__(self, model: ModelInfo, evidence: ReleaseEvidence, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("释放前证据确认")
        self.resize(850, 650)
        self.process = ""
        data = evidence.to_dict()
        processes = data.get("managed_processes", [])
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("必须核对以下实时证据；外部进程只展示，不会被终止。"))
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(_evidence_text(data))
        layout.addWidget(viewer, 1)
        self.process_combo = QComboBox()
        self.process_combo.addItem("不指定受管进程（调用 Provider 卸载 API）", "")
        for process in processes:
            self.process_combo.addItem(
                f"停止本工具进程 {process.get('id')} / PID {process.get('pid')} / {process.get('status')}",
                process.get("id"),
            )
        if len(processes) == 1:
            self.process_combo.setCurrentIndex(1)
        layout.addWidget(self.process_combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("确认安全释放")
        if model.provider == "local-file" and not processes:
            self.ok_button.setEnabled(False)
            self.process_combo.addItem("本地文件模型没有可安全停止的受管进程，已禁止释放", "")
        buttons.accepted.connect(self._accept_choice)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_choice(self) -> None:
        self.process = str(self.process_combo.currentData() or "")
        self.accept()


class ModelPage(QWidget):
    navigation_requested = Signal(str)
    t4_state_changed = Signal(object)
    status_message = Signal(str)
    # text, percent(-1=不确定), state(running/ok/error/cancelled)
    task_status = Signal(str, int, str)

    HEADERS = ("名称", "格式", "大小", "架构", "量化", "调用方", "所在文件夹", "后端")

    def __init__(self, store: ModelStore, registry: ProviderRegistry, service: ModelService, tasks: TaskManager):
        super().__init__()
        self.store = store
        self.registry = registry
        self.service = service
        self.tasks = tasks
        self.settings = Settings(store)
        self.t4: T4Provider = registry.get("t4")  # type: ignore[assignment]
        self._dispatch = GuiDispatch(self)
        self.t4_panel = T4Panel(self.t4, self)
        self.t4_panel.start_requested.connect(self.start_t4)
        self.t4_panel.stop_requested.connect(self.stop_t4)
        self.t4_panel.refresh_requested.connect(self.refresh_status)
        self.models: list[ModelInfo] = []
        self._model_by_path: dict[str, ModelInfo] = {}
        self._running_map: dict[str, list[RunningModel]] = {}
        self._evidence_map: dict[str, dict[str, Any]] = {}
        self._managed_loaded: set[str] = set()
        self._active_task_keys: set[str] = set()
        self._reclaim_queue: list[ModelInfo] = []
        self._reclaim_current: ModelInfo | None = None
        self._reclaim_active = False
        self._reclaim_total = 0
        self._t4_running: bool | None = None
        self._shutting_down = False
        self._build_ui()
        self.reload_models()
        cached = self.store.hardware.latest()
        if cached:
            self._display_hardware(cached.get("data", {}))
        self._warning_active = False
        self._silent_status = False
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_tick)
        self.apply_settings(self.settings.all())
        self.poll_timer.start()
        QTimer.singleShot(0, self._poll_tick)

    def _poll_tick(self) -> None:
        """周期轮询保持安静，避免每 5 秒覆盖侧栏里的任务结论。"""
        self._silent_status = True
        try:
            self.refresh_status()
        finally:
            self._silent_status = False

    @property
    def t4_running(self) -> bool | None:
        return self._t4_running

    def apply_settings(self, values: dict[str, Any]) -> None:
        try:
            self._poll_interval_ms = max(1000, min(3_600_000, int(values.get("status_poll_interval_ms", 5000))))
        except (TypeError, ValueError, OverflowError):
            self._poll_interval_ms = 5000
        self._poll_minimized_slowdown = bool(values.get("minimized_poll_slowdown", True))
        self._warning_enabled = bool(values.get("resource_warning_enabled", True))
        self._warning_sound = bool(values.get("resource_warning_sound", True))
        try:
            self._warning_percent = max(50, min(99, int(values.get("resource_warning_percent", 90))))
        except (TypeError, ValueError, OverflowError):
            self._warning_percent = 90
        heights = {"compact": 24, "comfortable": 40, "standard": 32}
        self.table.verticalHeader().setDefaultSectionSize(heights.get(str(values.get("density")), 32))
        self._update_poll_interval()

    def _update_poll_interval(self) -> None:
        if not hasattr(self, "poll_timer"):
            return
        multiplier = 4 if self._poll_minimized_slowdown and self.window().isMinimized() else 1
        self.poll_timer.setInterval(self._poll_interval_ms * multiplier)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_poll_interval()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(8)
        title_row = QHBoxLayout()
        title = QLabel("模型管理")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.hardware_label = QLabel("本机硬件：尚未采集")
        self.hardware_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.hardware_label.setStyleSheet("color: #93a1b5;")
        title_row.addWidget(self.hardware_label, 2)
        root.addLayout(title_row)

        toolbar = QHBoxLayout()
        self.scan_button = QPushButton("全盘扫描")
        self.refresh_button = QPushButton("刷新")
        self.reclaim_button = QPushButton("回收资源")
        self.load_button = QPushButton("装入")
        self.release_button = QPushButton("释放")
        self.delete_button = QPushButton("删除")
        self.copy_button = QPushButton("复制路径")
        self.config_button = QPushButton("扫描本机配置")
        for button in (
            self.scan_button, self.refresh_button, self.reclaim_button,
            self.load_button, self.release_button, self.delete_button,
            self.copy_button, self.config_button,
        ):
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        root.addLayout(toolbar)
        self.scan_button.clicked.connect(self.scan_all)
        self.refresh_button.clicked.connect(self.refresh_providers)
        self.reclaim_button.clicked.connect(self.reclaim_resources)
        self.load_button.clicked.connect(self.load_selected)
        self.release_button.clicked.connect(self.release_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        self.copy_button.clicked.connect(self.copy_selected_path)
        self.config_button.clicked.connect(self.scan_local_configuration)

        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索名称、路径或架构……")
        self.backend_combo = QComboBox()
        for text, value in (
            ("全部后端", ""), ("本地文件", "local-file"), ("Ollama", "ollama"),
            ("llama.cpp", "llamacpp"), ("LM Studio", "lmstudio"), ("vLLM", "vllm"), ("T4", "t4"),
        ):
            self.backend_combo.addItem(text, value)
        self.format_combo = QComboBox()
        self.status_combo = QComboBox()
        for text, value in (
            ("全部状态", "all"), ("已装入", "loaded"), ("未装入", "unloaded"),
            ("有调用方证据", "callers"), ("无已知调用方", "no-callers"),
        ):
            self.status_combo.addItem(text, value)
        self.count_label = QLabel("0 个模型")
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.backend_combo)
        filters.addWidget(self.format_combo)
        filters.addWidget(self.status_combo)
        filters.addWidget(self.count_label)
        root.addLayout(filters)
        self.search_edit.textChanged.connect(self.populate_table)
        self.backend_combo.currentIndexChanged.connect(self.populate_table)
        self.format_combo.currentIndexChanged.connect(self.populate_table)
        self.status_combo.currentIndexChanged.connect(self.populate_table)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.splitter.addWidget(self.table)

        details_panel = QGroupBox("模型详情")
        details_layout = QVBoxLayout(details_panel)
        details_layout.setContentsMargins(8, 10, 8, 8)
        self.details_view = QTextBrowser()
        self.details_view.setOpenExternalLinks(False)
        self.details_view.setPlaceholderText("选择模型后显示参数、上下文、量化、来源与最近使用。")
        details_layout.addWidget(self.details_view)
        self.splitter.addWidget(details_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([860, 340])
        root.addWidget(self.splitter, 1)
        self.table.itemSelectionChanged.connect(self.show_selected_details)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemDoubleClicked.connect(lambda _item: self.show_details_dialog())

        bottom_row = QHBoxLayout()
        status_card = QGroupBox("运行状态")
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(10, 8, 10, 8)
        self.status_card_label = QLabel("尚未读取运行状态。")
        self.status_card_label.setWordWrap(True)
        status_layout.addWidget(self.status_card_label)
        callers_card = QGroupBox("调用方明细")
        callers_layout = QVBoxLayout(callers_card)
        callers_layout.setContentsMargins(10, 8, 10, 8)
        self.callers_card_label = QLabel("尚未选择模型。")
        self.callers_card_label.setWordWrap(True)
        callers_layout.addWidget(self.callers_card_label)
        bottom_row.addWidget(status_card, 1)
        bottom_row.addWidget(callers_card, 1)
        root.addLayout(bottom_row)
        self._set_operation("已从本机缓存加载；首次启动不会自动全盘扫描。")
        self._update_t4_buttons()

    def _submit(
        self,
        key: str,
        worker: Callable[..., Any],
        *args: Any,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_progress: Callable[[Any], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
        quiet_if_running: bool = False,
        **worker_kwargs: Any,
    ) -> Task | None:
        if self._shutting_down:
            return None
        task = self.tasks.submit(key, worker, *args, **worker_kwargs)
        if task is None:
            if not quiet_if_running:
                self._set_operation(f"任务 {key} 已在运行，本次未重复提交。", busy=True)
            return None
        self._active_task_keys.add(key)
        self._update_busy_indicator()
        dispatch = self._dispatch

        def deliver(callback: Callable[..., Any], *values: Any) -> None:
            if self._shutting_down:
                return

            def run() -> None:
                if not self._shutting_down:
                    callback(*values)

            dispatch.post(run)

        progress_callback = on_progress or self._display_progress
        task.signals.progress.connect(lambda value: deliver(progress_callback, value))
        if on_result is not None:
            task.signals.result.connect(lambda value: deliver(on_result, value))
        error_callback = on_error or (lambda exc, name=key: self._show_task_error(name, exc))
        task.signals.error.connect(lambda error: deliver(error_callback, error))
        cancelled_callback = on_cancelled or (lambda name=key: self._set_operation(f"任务已取消：{name}"))
        task.signals.cancelled.connect(lambda: deliver(cancelled_callback))

        def finished() -> None:
            self._active_task_keys.discard(key)
            self._update_busy_indicator()
            self._update_t4_buttons()
            if on_finished is not None:
                on_finished()

        task.signals.finished.connect(lambda: dispatch.post(finished))
        return task

    def _update_busy_indicator(self) -> None:
        if hasattr(self, "t4_panel"):
            busy = bool({"t4-start", "t4-stop"} & self._active_task_keys)
            self.t4_panel.set_busy(busy, self._shutting_down)
            if not busy and not self._shutting_down:
                self.t4_panel.set_running(self._t4_running)
        self.schedule_button_state()

    def schedule_button_state(self) -> None:
        """忙时保守禁用扫描按钮，结束后恢复。"""
        if not hasattr(self, "scan_button"):
            return
        busy = "model-scan" in self._active_task_keys
        self.scan_button.setEnabled(not busy)

    def _set_operation(self, message: str, *, busy: bool | None = None, error: bool = False) -> None:
        state = "running" if busy else ("error" if error else "ok")
        self.status_message.emit(message)
        self.task_status.emit(message, -1, state)

    def _display_progress(self, value: Any) -> None:
        if isinstance(value, dict):
            message = str(value.get("message") or phase_text(value.get("phase")) or "后台任务进行中")
            if value.get("provider"):
                message += f" / {value['provider']}"
        else:
            phase = phase_text(getattr(value, "phase", ""))
            message = (
                f"扫描 · {phase}：已遍历 {getattr(value, 'files_scanned', 0)} 个文件，"
                f"解析 {getattr(value, 'files_parsed', 0)}，发现 {getattr(value, 'models_found', 0)} 个模型"
            )
        self._set_operation(message, busy=True)

    def _show_task_error(self, name: str, error: Exception) -> None:
        self._set_operation(f"{name} 失败：{error}", error=True)

    def reload_models(self) -> None:
        if self._shutting_down:
            return
        selected = self.selected_model()
        self.models = self.store.all()
        self._model_by_path = {model.file_path: model for model in self.models}
        rows = self.store.connection.execute(
            "SELECT model_id, favorite, pinned, tags, notes FROM model_prefs"
        ).fetchall()
        self._model_prefs = {row["model_id"]: dict(row) for row in rows}
        current_format = self.format_combo.currentData()
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        self.format_combo.addItem("全部格式", "")
        for value in sorted({model.format or "unknown" for model in self.models}):
            self.format_combo.addItem(value, value)
        index = self.format_combo.findData(current_format)
        self.format_combo.setCurrentIndex(max(0, index))
        self.format_combo.blockSignals(False)
        self.populate_table()
        if selected and selected.file_path in self._model_by_path:
            self._select_path(selected.file_path)

    def selected_model(self) -> ModelInfo | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        return self._model_by_path.get(str(item.data(_MODEL_ROLE)))

    def _select_path(self, path: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(_MODEL_ROLE) == path:
                self.table.selectRow(row)
                return

    def _is_loaded(self, model: ModelInfo) -> bool:
        return model.model_id in self._managed_loaded or bool(self._running_map.get(model.model_id))

    def _matches_filters(self, model: ModelInfo) -> bool:
        query = self.search_edit.text().strip().casefold()
        haystack = " ".join((
            model.display_name, model.file_name, model.file_path,
            model.architecture, model.model_ref, model.provider,
        )).casefold()
        if query and query not in haystack:
            return False
        backend = self.backend_combo.currentData()
        if backend and model.provider != backend:
            return False
        model_format = self.format_combo.currentData()
        if model_format and (model.format or "unknown") != model_format:
            return False
        status = self.status_combo.currentData()
        loaded = self._is_loaded(model)
        has_callers = bool(_callers(self._evidence_map.get(model.model_id, {})))
        if status == "loaded" and not loaded:
            return False
        if status == "unloaded" and loaded:
            return False
        if status == "callers" and not has_callers:
            return False
        if status == "no-callers" and has_callers:
            return False
        return True

    def populate_table(self) -> None:
        if not hasattr(self, "table"):
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        visible = [model for model in self.models if self._matches_filters(model)]
        visible.sort(key=lambda model: (
            not bool(self._model_prefs.get(model.model_id, {}).get("pinned")),
            not bool(self._model_prefs.get(model.model_id, {}).get("favorite")),
            (model.display_name or model.file_name).casefold(),
        ))
        self.table.setRowCount(len(visible))
        for row, model in enumerate(visible):
            loaded = self._is_loaded(model)
            callers = _callers(self._evidence_map.get(model.model_id, {}))
            if "://" in model.file_path:
                folder = model.source_dir or "后端虚拟 URI（无本地目录）"
            else:
                folder = str(Path(model.file_path).parent)
            caller_text = f"{len(callers)} 个进程证据" if callers else ("无已知调用方" if loaded else "—")
            prefs = self._model_prefs.get(model.model_id, {})
            flags = [name for name, enabled in (("置顶", prefs.get("pinned")), ("收藏", prefs.get("favorite"))) if enabled]
            display_name = ("[" + "/".join(flags) + "] " if flags else "") + (model.display_name or model.file_name)
            values = (
                display_name,
                model.format or "unknown",
                model.size_human,
                model.architecture or "—",
                model.quant or "—",
                caller_text,
                folder,
                model.provider or "unknown",
            )
            foreground = QBrush(_CALLER_COLOR if callers else (_LOADED_COLOR if loaded else _MUTED_COLOR))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setForeground(foreground)
                if column == 0:
                    item.setData(_MODEL_ROLE, model.file_path)
                    item.setToolTip(f"model_id: {model.model_id}\n{model.file_path}")
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.count_label.setText(f"{len(visible)} / {len(self.models)} 个模型")
        self.show_selected_details()

    def show_selected_details(self) -> None:
        model = self.selected_model()
        if model is None:
            self.details_view.setPlainText("未选择模型。名称列仅显示模型名，完整路径会在这里显示。")
            return
        running = self._running_map.get(model.model_id, [])
        evidence = self._evidence_map.get(model.model_id, {})
        callers = _callers(evidence)
        try:
            usage = self.store.usage.daily(18250, model.model_id)
            last_used = max((row["day"] for row in usage), default="无记录")
        except Exception:
            last_used = "读取失败"
        prefs = self.store.prefs.get(model.model_id)
        lines = [
            f"名称：{model.display_name or model.file_name}",
            f"偏好：收藏={prefs['favorite']}；置顶={prefs['pinned']}；标签={', '.join(prefs['tags']) or '无'}",
            f"备注：{prefs['notes'] or '无'}",
            f"完整路径 / URI：{model.file_path}",
            f"参数量：{model.params_b:g} B" if model.params_b else "参数量：未知",
            f"上下文：{model.context_length or '未知'}；量化：{model.quant or '未知'}；架构：{model.architecture or '未知'}",
            f"许可证：{model.license or '未知'}；完整性：{model.integrity} {model.integrity_error}",
            f"来源：{model.source_type or model.provider}；扫描来源：{model.source_dir or '未报告'}；最近使用：{last_used}",
            f"运行状态：{'已装入' if self._is_loaded(model) else '未检测到装入'}；后端运行记录：{len(running)} 条",
        ]
        for item in running:
            lines.append(
                f"  - {item.provider}/{item.name}：内存 {item.size_human}，显存 {item.vram_human}，"
                f"expires_at={item.expires_at or '未报告'}"
            )
        if model.model_id in self._managed_loaded and not running:
            lines.append("  - 本会话已收到成功装入/受管进程启动结果，等待下一轮 Provider 状态确认。")
        lines.append(f"调用方明细：{len(callers)} 个进程证据")
        for user in callers:
            lines.append(
                f"  - PID={user.get('pid')} {user.get('name') or '未知'}；"
                f"exe={user.get('exe_path') or '未知'}；provider={user.get('provider') or ''}；"
                f"port={user.get('target_port') or ''}"
            )
        if evidence:
            for inspection in evidence.get("inspections", []):
                result = inspection.get("result", {})
                if result.get("message"):
                    lines.append(f"证据说明：{result['message']}")
                for error in result.get("errors", []):
                    lines.append(f"证据错误：{error}")
        self.details_view.setPlainText("\n".join(lines))
        self._update_bottom_cards(model, running, callers)

    def _update_bottom_cards(
        self,
        model: ModelInfo | None = None,
        running: list[RunningModel] | None = None,
        callers: list[dict[str, Any]] | None = None,
    ) -> None:
        model = model if model is not None else self.selected_model()
        running = running if running is not None else (
            self._running_map.get(model.model_id, []) if model else []
        )
        callers = callers if callers is not None else (
            _callers(self._evidence_map.get(model.model_id, {})) if model else []
        )
        loaded_models = [item for item in self.models if self._is_loaded(item)]
        status_lines = [
            f"已装入模型：{len(loaded_models)} 个；后端运行记录：{len(self._running_map)} 个模型。"
        ]
        if model is not None:
            status_lines.append(
                f"当前：{model.display_name or model.file_name} · "
                f"{'已装入' if self._is_loaded(model) else '未装入'}"
            )
            for item in running:
                status_lines.append(
                    f"{item.provider}/{item.name}：内存 {item.size_human}，显存 {item.vram_human}"
                )
        self.status_card_label.setText("\n".join(status_lines))
        if model is None:
            self.callers_card_label.setText("选择模型后显示调用方证据。")
        elif not callers:
            self.callers_card_label.setText("未发现调用或占用进程。")
        else:
            self.callers_card_label.setText("\n".join(
                f"PID={user.get('pid')} {user.get('name') or '未知'}；"
                f"端口={user.get('target_port') or '—'}"
                for user in callers[:6]
            ))

    def _require_model(self) -> ModelInfo | None:
        model = self.selected_model()
        if model is None:
            QMessageBox.information(self, "未选择模型", "请先在模型表中选择一行。")
        return model

    def scan_all(self) -> None:
        dialog = ScanScopeDialog(
            self.store, self.service.scanner.available_drives(), self.settings, self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        roots = dialog.checked_roots()
        if not roots:
            return
        self._start_scan(roots=roots, include_ollama=dialog.include_ollama())

    def _start_scan(self, *, roots: list[str], include_ollama: bool) -> None:
        self.scan_button.setEnabled(False)
        self._set_operation("扫描任务已提交，可继续浏览本机缓存。", busy=True)

        def done(_result: Any = None) -> None:
            self.scan_button.setEnabled(True)

        self._submit(
            "model-scan", self.service.scan,
            roots=roots, include_ollama=include_ollama,
            on_result=self._on_scan_result,
            on_finished=done,
        )

    def _on_scan_result(self, result: Any) -> None:
        self.reload_models()
        message = (
            f"扫描完成：发现 {len(result.models)} 个模型，遍历 {result.files_scanned} 个文件，"
            f"问题 {len(result.issues)} 条，清理失效登记 {result.pruned} 条。"
        )
        self._set_operation(message)
        if result.issues:
            details = "\n".join(f"{issue.path}: {issue.message}" for issue in result.issues)
            TextDialog("扫描完成，但存在真实问题", message + "\n\n" + details, self).exec()

    def refresh_providers(self) -> None:
        self.refresh_button.setEnabled(False)
        self._submit(
            "provider-refresh", self.service.refresh_models,
            on_result=self._on_provider_refresh,
            on_finished=lambda: self.refresh_button.setEnabled(True),
        )
        self.refresh_status()

    def _on_provider_refresh(self, result: dict[str, Any]) -> None:
        self.reload_models()
        errors = result.get("errors", {})
        message = f"Provider 刷新得到 {len(result.get('models', []))} 条记录。"
        if errors:
            message += " 失败：" + "；".join(f"{key}: {value}" for key, value in errors.items())
        self._set_operation(message, error=bool(errors))

    def refresh_status(self) -> None:
        self.t4_panel.refresh_button.setEnabled(False)
        self._submit(
            "status-refresh", _status_worker, self.service,
            on_result=self._on_status,
            on_error=self._on_status_error,
            quiet_if_running=True,
        )

    def _on_status(self, result: dict[str, Any]) -> None:
        self._running_map.clear()
        for model in self.models:
            matched = [run for run in result.get("running", []) if _running_matches(model, run)]
            if matched:
                self._running_map[model.model_id] = matched
        self._evidence_map = result.get("evidence", {})
        statuses = result.get("providers", {})
        t4_status = statuses.get("t4")
        if t4_status is not None:
            self._display_t4_status(t4_status)
        errors = result.get("errors", {})
        loaded = len(result.get("running", []))
        message = f"状态已刷新：Provider 报告 {loaded} 个运行模型；下次按设置轮询。"
        if errors:
            message += " 错误：" + "；".join(f"{key}: {value}" for key, value in errors.items())
        if not self._silent_status:
            self._set_operation(message, error=bool(errors))
        self.populate_table()
        self._update_bottom_cards()

    def _on_status_error(self, error: Exception) -> None:
        if not self._silent_status:
            self._set_operation(f"状态刷新失败（保留上次已知状态）：{error}", error=True)
        self.t4_panel.service_label.setToolTip(str(error))

    def refresh_hardware(self, manual: bool = False) -> None:
        self.config_button.setEnabled(False)
        self._submit(
            "hardware-scan", _hardware_worker, self.store,
            on_result=lambda snapshot: self._on_hardware(snapshot, manual),
            on_finished=lambda: self.config_button.setEnabled(True),
            quiet_if_running=not manual,
        )

    def _on_hardware(self, snapshot: HardwareSnapshot, manual: bool) -> None:
        self._display_hardware(snapshot.to_dict())
        self._set_operation("硬件快照已采集并缓存到 SQLite。")
        if manual:
            TextDialog("本机配置扫描结果", self._hardware_text(snapshot.to_dict(), full=True), self).exec()

    def _display_hardware(self, data: dict[str, Any]) -> None:
        cpu = data.get("cpu", {})
        memory = data.get("memory", {})
        gpus = data.get("gpus", [])
        gpu_names = "、".join(str(item.get("name") or "未知显卡") for item in gpus) or "未获得显卡信息"
        self.hardware_label.setText(
            f"CPU {cpu.get('physical_cores') or '?'} 核 · "
            f"内存 {human_size(memory.get('total_bytes', 0))} · GPU {gpu_names}"
        )
        self.hardware_label.setToolTip(self._hardware_text(data, full=True))

    def _hardware_text(self, data: dict[str, Any], *, full: bool = False) -> str:
        cpu = data.get("cpu", {})
        memory = data.get("memory", {})
        ollama = data.get("ollama", {})
        lines = [
            f"CPU：{cpu.get('name') or '未知'}；{cpu.get('physical_cores') or '?'} 物理核 / {cpu.get('logical_cores') or '?'} 逻辑核",
            f"内存：已用 {human_size(memory.get('used_bytes', 0))} / {human_size(memory.get('total_bytes', 0))}；"
            f"可用 {human_size(memory.get('available_bytes', 0))}（{memory.get('percent', 0)}%）",
            f"Ollama：{'运行中' if ollama.get('running') else '未运行/未确认'}；"
            f"版本 {ollama.get('version') or '未知'}；{ollama.get('base_url') or 'http://127.0.0.1:11434'}；不会代装或启动",
        ]
        gpus = data.get("gpus", [])
        if gpus:
            for gpu in gpus:
                total = gpu.get("total_vram_bytes")
                free = gpu.get("free_vram_bytes")
                lines.append(
                    f"GPU：{gpu.get('name') or '未知'}；总显存 {human_size(total) if total is not None else '未知'}；"
                    f"空闲 {human_size(free) if free is not None else '未知'}；来源 {gpu.get('source') or '未知'}"
                )
        else:
            lines.append("GPU：未获得显卡信息（不等于不存在）。")
        if full:
            captured = data.get("captured_at")
            if captured:
                lines.append(f"采集时间：{datetime.fromtimestamp(captured).isoformat(timespec='seconds')}")
            for warning in data.get("warnings", []):
                lines.append(f"警告：{warning}")
            for error in data.get("errors", []):
                lines.append(f"错误：{error}")
            lines.append("\nProvider 状态：")
            for name in self.registry.names:
                lines.append(f"- {name}（检测只连接本机既有服务，不自动启动）")
        return "\n".join(lines)

    def scan_local_configuration(self) -> None:
        self.refresh_hardware(manual=True)
        self.refresh_status()

    def load_selected(self) -> None:
        model = self._require_model()
        if model is None:
            return
        if model.provider == "t4":
            self.start_t4()
            return
        dialog = LoadModelDialog(model, self.service, self.tasks, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        options = dialog.load_options()
        self.load_button.setEnabled(False)
        self._set_operation(f"正在提交 {model.display_name or model.file_name} 的真实装入任务。", busy=True)
        self._submit(
            f"model-load:{model.model_id}", self.service.load_model_task,
            model_id=model.file_path,
            on_result=lambda result, m=model: self._on_load_result(m, result),
            on_finished=lambda: self.load_button.setEnabled(True),
            **options,
        )

    def _on_load_result(self, model: ModelInfo, result: OperationResult) -> None:
        if result.success:
            self._managed_loaded.add(model.model_id)
            self._set_operation(result.message or "Provider 已确认装入。")
        else:
            self._set_operation(result.message or result.error or "装入失败。", error=True)
            QMessageBox.critical(self, "装入未成功", result.message or result.error)
        self.populate_table()
        self.refresh_status()

    def release_selected(self) -> None:
        if self._reclaim_active:
            return
        model = self._require_model()
        if model is not None:
            self._begin_release(model)

    def _is_reclaim_current(self, model: ModelInfo) -> bool:
        return self._reclaim_active and self._reclaim_current is model

    def _begin_release(self, model: ModelInfo) -> None:
        if model.provider == "t4":
            self.stop_t4()
            return
        self.release_button.setEnabled(False)
        task = self._submit(
            f"release-inspect:{model.model_id}", _inspect_worker,
            self.service, model.file_path,
            on_result=lambda evidence, m=model: self._show_release_evidence(m, evidence),
            on_error=lambda error, m=model: self._on_release_task_error("证据收集", m, error),
            on_cancelled=lambda m=model: self._on_release_cancelled(m),
            on_finished=self._on_release_task_finished,
        )
        if task is None and self._is_reclaim_current(model):
            self._abort_reclaim("证据收集任务未能提交；回收队列已停止，后续模型未处理。", error=True)

    def _on_release_task_finished(self) -> None:
        if not self._reclaim_active and not self._shutting_down:
            self.release_button.setEnabled(True)

    def _on_release_task_error(self, stage: str, model: ModelInfo, error: Exception) -> None:
        name = model.display_name or model.file_name
        message = f"{name} 的{stage}失败：{error}"
        if self._is_reclaim_current(model):
            self._abort_reclaim(message + "；回收队列已停止，后续模型未处理。", error=True)
        else:
            self._set_operation(message, error=True)

    def _on_release_cancelled(self, model: ModelInfo) -> None:
        if self._is_reclaim_current(model):
            self._abort_reclaim("释放任务已取消；回收队列已停止，后续模型未处理。")
        else:
            self._set_operation("释放任务已取消；未继续执行后续操作。")

    def _show_release_evidence(self, model: ModelInfo, evidence: ReleaseEvidence) -> None:
        dialog = ReleaseDialog(model, evidence, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            if self._is_reclaim_current(model):
                self._abort_reclaim("已取消当前模型的证据确认；回收队列已停止，后续模型未处理。")
            else:
                self._set_operation("已取消释放；未调用卸载，也未终止任何进程。")
            return
        process = dialog.process or None
        self.release_button.setEnabled(False)
        self._set_operation("证据已确认，正在释放；不会终止非受管进程。", busy=True)
        task = self._submit(
            f"model-release:{model.model_id}", _release_worker,
            self.service, model.file_path, process,
            on_result=lambda result, m=model: self._on_release_result(m, result),
            on_error=lambda error, m=model: self._on_release_task_error("释放", m, error),
            on_cancelled=lambda m=model: self._on_release_cancelled(m),
            on_finished=self._on_release_task_finished,
        )
        if task is None and self._is_reclaim_current(model):
            self._abort_reclaim("释放任务未能提交；回收队列已停止，后续模型未处理。", error=True)

    def _on_release_result(self, model: ModelInfo, result: OperationResult) -> None:
        queued = self._is_reclaim_current(model)
        if result.success:
            self._managed_loaded.discard(model.model_id)
            if queued:
                completed = self._reclaim_total - len(self._reclaim_queue)
                self._set_operation(
                    f"第 {completed}/{self._reclaim_total} 个模型释放成功："
                    f"{model.display_name or model.file_name}。"
                )
                self._reclaim_current = None
                self.populate_table()
                self._advance_reclaim()
                return
            self._set_operation(result.message or "释放请求已成功。")
        else:
            message = result.message or result.error or "释放失败。"
            if queued:
                self._abort_reclaim(message + "；回收队列已停止，后续模型未处理。", error=True)
            else:
                self._set_operation(message, error=True)
            QMessageBox.critical(self, "释放未成功", message)
        self.populate_table()
        self.refresh_status()

    def _clear_reclaim(self) -> None:
        self._reclaim_queue.clear()
        self._reclaim_current = None
        self._reclaim_active = False
        self._reclaim_total = 0

    def _abort_reclaim(self, message: str, *, error: bool = False) -> None:
        self._clear_reclaim()
        if not self._shutting_down:
            self.reclaim_button.setEnabled(True)
            self.release_button.setEnabled(True)
        self._set_operation(message, error=error)

    def _advance_reclaim(self) -> None:
        if not self._reclaim_active or self._shutting_down:
            return
        if not self._reclaim_queue:
            total = self._reclaim_total
            self._clear_reclaim()
            self.reclaim_button.setEnabled(True)
            self.release_button.setEnabled(True)
            self._set_operation(f"顺序回收完成：{total} 个模型均已逐项确认并由 Provider 返回成功。")
            self.refresh_status()
            return
        model = self._reclaim_queue.pop(0)
        self._reclaim_current = model
        position = self._reclaim_total - len(self._reclaim_queue)
        self._set_operation(
            f"正在处理第 {position}/{self._reclaim_total} 个模型："
            f"{model.display_name or model.file_name}；先单独收集占用证据。",
            busy=True,
        )
        self._begin_release(model)

    def reclaim_resources(self) -> None:
        if self._reclaim_active:
            return
        candidates = [
            model for model in self.models
            if self._is_loaded(model) and model.provider != "t4"
        ]
        if not candidates:
            QMessageBox.information(
                self,
                "回收资源",
                "当前状态缓存中没有可逐个回收的非 T4 已装入模型。T4 请使用其专属停止按钮。",
            )
            return
        names = "\n".join(f"- {model.display_name or model.file_name} ({model.provider})" for model in candidates)
        answer = QMessageBox.question(
            self,
            "确认回收范围",
            f"范围仅限以下 {len(candidates)} 个已装入模型：\n{names}\n\n"
            "接下来会逐个重新收集并展示占用证据，每个模型都需再次确认；任一取消或失败都会停止并清空队列。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._reclaim_queue = list(candidates)
            self._reclaim_current = None
            self._reclaim_total = len(candidates)
            self._reclaim_active = True
            self.reclaim_button.setEnabled(False)
            self.release_button.setEnabled(False)
            self._advance_reclaim()

    def delete_selected(self) -> None:
        model = self._require_model()
        if model is None:
            return
        local = model.provider == "local-file"
        method = "本地文件及分片将移入应用隔离区，不直接 unlink。" if local else "通过所属 Provider API 删除后端模型，不触碰本地 manifest/blob。"
        answer = QMessageBox.warning(
            self,
            "明确确认删除",
            f"模型：{model.display_name or model.file_name}\n路径 / URI：{model.file_path}\n\n{method}\n\n确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.delete_button.setEnabled(False)
        self._submit(
            f"model-delete:{model.model_id}", _delete_worker,
            self.service, model,
            on_result=self._on_delete_result,
            on_finished=lambda: self.delete_button.setEnabled(True),
        )

    def _on_delete_result(self, result: OperationResult) -> None:
        self._set_operation(result.message or result.error, error=not result.success)
        if result.success:
            self.reload_models()
            self.refresh_providers()
        else:
            QMessageBox.critical(self, "删除未成功", result.message or result.error)

    def copy_selected_path(self) -> None:
        model = self._require_model()
        if model is None:
            return
        QApplication.clipboard().setText(model.file_path)
        if "://" in model.file_path:
            self._set_operation("已复制后端虚拟 URI；它不是本地文件路径，不能用于打开目录。")
        else:
            self._set_operation("已复制本地模型完整路径。")

    def open_selected_directory(self) -> None:
        model = self._require_model()
        if model is None:
            return
        if "://" in model.file_path:
            QMessageBox.information(
                self,
                "虚拟 URI 无本地目录",
                f"{model.file_path}\n\n这是后端虚拟 URI，不能假装存在本地目录；可使用“复制路径”复制 URI。",
            )
            return
        folder = Path(model.file_path).parent
        if not folder.is_dir():
            QMessageBox.warning(self, "目录不可用", f"模型所在目录当前不可访问：\n{folder}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(self, "打开失败", f"系统未能打开目录：\n{folder}")

    def show_details_dialog(self) -> None:
        model = self._require_model()
        if model is not None:
            self.show_selected_details()
            TextDialog("模型详情 / 运行状态 / 调用方", self.details_view.toPlainText(), self).exec()

    def _show_context_menu(self, position: Any) -> None:
        if self.table.itemAt(position) is None:
            return
        self.table.setCurrentCell(self.table.itemAt(position).row(), 0)
        menu = QMenu(self)
        menu.addAction("装入", self.load_selected)
        menu.addAction("下载（转到资源下载）", lambda: self.navigation_requested.emit("downloads"))
        menu.addAction("打开目录", self.open_selected_directory)
        menu.addAction("复制路径", self.copy_selected_path)
        menu.addSeparator()
        menu.addAction("详情", self.show_details_dialog)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _display_t4_scripts(self) -> None:
        self.t4_panel.set_scripts(self.t4.start_script, self.t4.stop_script)

    def _display_t4_status(self, status: ProviderStatus) -> None:
        previous = self._t4_running
        self._t4_running = bool(status.running)
        self.t4_panel.set_status(status)
        self._display_t4_scripts()
        gpu = status.details.get("gpu", {})
        rows = gpu.get("gpus", [])
        self._check_resource_warning(rows)
        if previous != self._t4_running:
            self.t4_state_changed.emit(self._t4_running)
        self._update_t4_buttons()

    def _check_resource_warning(self, rows: Any) -> None:
        warnings: list[str] = []
        if self._warning_enabled and isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    total = int(row.get("memory_total_mib", 0))
                    used = int(row.get("memory_used_mib", 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if total <= 0:
                    continue
                percent = used * 100.0 / total
                if percent >= self._warning_percent:
                    warnings.append(
                        f"GPU{row.get('index', '?')} {row.get('name', 'Tesla T4')} 显存占用 {percent:.1f}%"
                    )
        self.t4_panel.gpu_label.setStyleSheet("color: #ff7373;" if warnings else "")
        if warnings and not self._warning_active:
            message = "显存预警：" + "；".join(warnings) + f"（阈值 {self._warning_percent}%）"
            self.status_message.emit(message)
            if self._warning_sound:
                QApplication.beep()
        self._warning_active = bool(warnings)

    def _update_t4_buttons(self) -> None:
        if not hasattr(self, "t4_panel"):
            return
        busy = bool({"t4-start", "t4-stop", "status-refresh"} & self._active_task_keys)
        self.t4_panel.set_busy(busy, self._shutting_down)
        if not busy and not self._shutting_down:
            self.t4_panel.set_running(self._t4_running)

    def start_t4(self) -> bool:
        answer = QMessageBox.question(
            self,
            "明确手动启动 T4",
            f"这是固定 T4 服务的手动启动，不走通用 load_model。\n\n"
            f"将执行 Provider 默认持有的脚本：\n{self.t4.start_script}\n\n"
            "只允许 Tesla T4，排除 GPU0/GTX1080；不会设置开机自启。确认执行？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._set_operation("已明确确认，正在执行 T4 启动脚本并等待真实健康检查。", busy=True)
        task = self._submit(
            "t4-start", self.t4.start_service,
            on_result=lambda result: self._on_t4_result("启动", result),
        )
        self._update_t4_buttons()
        return task is not None

    def stop_t4(
        self,
        _checked: bool = False,
        *,
        confirmed: bool = False,
        report: bool = True,
        on_complete: Callable[[OperationResult], None] | None = None,
    ) -> bool:
        if not confirmed:
            answer = QMessageBox.question(
                self,
                "明确手动停止 T4",
                f"将执行 Provider 默认持有的停止脚本：\n{self.t4.stop_script}\n\n"
                "只有脚本返回码、/health 与独立显存检查结果符合时才报告成功。确认停止？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self._set_operation("已明确确认，正在停止 T4 并核验真实状态。", busy=True)
        task = self._submit(
            "t4-stop", self.t4.stop_service,
            on_result=lambda result: self._on_t4_result("停止", result, report=report, on_complete=on_complete),
            on_error=lambda exc: self._on_t4_task_error(exc, report=report, on_complete=on_complete),
        )
        self._update_t4_buttons()
        return task is not None

    def _on_t4_result(
        self,
        action: str,
        result: OperationResult,
        *,
        report: bool = True,
        on_complete: Callable[[OperationResult], None] | None = None,
    ) -> None:
        if result.success:
            running = action == "启动"
            previous = self._t4_running
            self._t4_running = running
            if previous != running:
                self.t4_state_changed.emit(running)
        self._set_operation(result.message or result.error, error=not result.success)
        if report and not result.success:
            QMessageBox.critical(self, f"T4 {action}未成功", result.message or result.error)
        self._update_t4_buttons()
        if on_complete is not None:
            on_complete(result)
        self.refresh_status()

    def _on_t4_task_error(
        self,
        error: Exception,
        *,
        report: bool,
        on_complete: Callable[[OperationResult], None] | None,
    ) -> None:
        self._set_operation(f"T4 操作异常：{error}", error=True)
        if report:
            QMessageBox.critical(self, "T4 操作异常", str(error))
        if on_complete is not None:
            on_complete(OperationResult(False, f"T4 操作异常：{error}", error=str(error)))

    def shutdown(self) -> None:
        self._shutting_down = True
        if hasattr(self, "poll_timer"):
            self.poll_timer.stop()
        self._clear_reclaim()
        self._active_task_keys.clear()
        self._update_busy_indicator()
