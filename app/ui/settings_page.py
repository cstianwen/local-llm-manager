"""设置页：真实持久化、模型偏好、统计对比、数据传输与日志。"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..catalog.service import CatalogService, CatalogSnapshot
from ..core.config import APP_VERSION
from ..core.models import OperationResult, human_size
from ..core.settings import Settings
from ..core.tasks import TaskManager
from ..services.model_service import ModelService
from ..store.db import ModelStore


_APPEARANCE_KEYS = (
    "theme", "accent_color", "window_opacity", "font_family", "font_size",
    "font_weight", "density", "icon_size", "panel_radius",
)
_CATALOG_TASK_KEY = "settings:catalog-refresh"


def _combo_index(combo: QComboBox, data: Any) -> int:
    return max(0, combo.findData(data))


def _table(headers: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class SettingsPage(QWidget):
    status_message = Signal(str)
    settings_changed = Signal(dict)
    models_changed = Signal()

    def __init__(
        self,
        store: ModelStore,
        settings: Settings,
        service: ModelService,
        catalog: CatalogService,
        tasks: TaskManager,
        t4_panel: QWidget | None = None,
    ):
        super().__init__()
        self.store = store
        self.settings = settings
        self.service = service
        self.catalog = catalog
        self.tasks = tasks
        self._t4_panel = t4_panel
        self._models = []
        self._loading = False
        self._shutting_down = False

        root = QVBoxLayout(self)
        title = QLabel("设置")
        title.setStyleSheet("font-size: 22px; font-weight: 700;")
        root.addWidget(title)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_general_tab()
        self._build_models_tab()
        self._build_statistics_tab()
        self._build_data_tab()
        version = QLabel(f"本地模型管家 v{APP_VERSION} · 设置与数据保存在本机 SQLite")
        version.setAlignment(Qt.AlignmentFlag.AlignRight)
        root.addWidget(version)

        self.reload_models()
        self._load_settings()
        self.refresh_presets()
        self.refresh_statistics()
        self.refresh_logs()

    def _new_tab(self, title: str) -> QVBoxLayout:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        self.tabs.addTab(area, title)
        return layout

    def _build_general_tab(self) -> None:
        layout = self._new_tab("常规与界面")
        runtime = QGroupBox("状态轮询与显存预警")
        form = QFormLayout(runtime)
        self.poll_seconds = QSpinBox()
        self.poll_seconds.setRange(1, 3600)
        self.poll_seconds.setSuffix(" 秒")
        form.addRow("状态刷新间隔", self.poll_seconds)
        self.minimized_slowdown_check = QCheckBox("窗口最小化时按 4 倍间隔降频")
        form.addRow("后台降频", self.minimized_slowdown_check)
        self.warning_enabled_check = QCheckBox("启用 Tesla T4 实际显存占用预警")
        form.addRow("显存预警", self.warning_enabled_check)
        self.warning_percent_spin = QSpinBox()
        self.warning_percent_spin.setRange(50, 99)
        self.warning_percent_spin.setSuffix(" %")
        form.addRow("预警阈值", self.warning_percent_spin)
        self.warning_sound_check = QCheckBox("达到阈值时播放系统提示音（仅在状态变化时提醒）")
        form.addRow("提示音", self.warning_sound_check)
        layout.addWidget(runtime)
        if self._t4_panel is not None:
            layout.addWidget(self._t4_panel)

        appearance = QGroupBox("界面外观（主窗口与 Agent 窗同步）")
        form = QFormLayout(appearance)
        self.theme_combo = QComboBox()
        for text, value in (("跟随系统", "system"), ("深色", "dark"), ("浅色", "light")):
            self.theme_combo.addItem(text, value)
        form.addRow("主题模式", self.theme_combo)
        self.accent_combo = QComboBox()
        for text, value in (
            ("蓝色", "#4f8cff"), ("紫色", "#9065d8"), ("青色", "#159da8"),
            ("绿色", "#3b9b62"), ("橙色", "#d17a28"), ("红色", "#cf5555"),
        ):
            self.accent_combo.addItem(text, value)
        form.addRow("强调色", self.accent_combo)
        opacity_row = QWidget()
        opacity_layout = QHBoxLayout(opacity_row)
        opacity_layout.setContentsMargins(0, 0, 0, 0)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(70, 100)
        self.opacity_label = QLabel()
        opacity_layout.addWidget(self.opacity_slider, 1)
        opacity_layout.addWidget(self.opacity_label)
        form.addRow("窗口不透明度", opacity_row)
        self.font_family_combo = QFontComboBox()
        form.addRow("字体族", self.font_family_combo)
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(9, 24)
        self.font_size_spin.setSuffix(" px")
        form.addRow("字号", self.font_size_spin)
        self.font_weight_combo = QComboBox()
        for text, value in (("常规", 400), ("中等", 500), ("半粗", 600), ("粗体", 700)):
            self.font_weight_combo.addItem(text, value)
        form.addRow("字体粗细", self.font_weight_combo)
        self.density_combo = QComboBox()
        for text, value in (("紧凑", "compact"), ("标准", "standard"), ("宽松", "comfortable")):
            self.density_combo.addItem(text, value)
        form.addRow("行密度", self.density_combo)
        self.icon_size_spin = QSpinBox()
        self.icon_size_spin.setRange(12, 48)
        self.icon_size_spin.setSuffix(" px")
        form.addRow("图标大小", self.icon_size_spin)
        self.panel_radius_spin = QSpinBox()
        self.panel_radius_spin.setRange(0, 20)
        self.panel_radius_spin.setSuffix(" px")
        form.addRow("面板圆角", self.panel_radius_spin)
        self.reset_appearance_button = QPushButton("恢复默认外观")
        form.addRow("恢复默认", self.reset_appearance_button)
        layout.addWidget(appearance)
        layout.addStretch(1)

        self.poll_seconds.valueChanged.connect(self._save_runtime)
        self.minimized_slowdown_check.toggled.connect(self._save_runtime)
        self.warning_enabled_check.toggled.connect(self._save_runtime)
        self.warning_percent_spin.valueChanged.connect(self._save_runtime)
        self.warning_sound_check.toggled.connect(self._save_runtime)
        self.theme_combo.currentIndexChanged.connect(self._save_appearance)
        self.accent_combo.currentIndexChanged.connect(self._save_appearance)
        self.opacity_slider.valueChanged.connect(self._save_appearance)
        self.font_family_combo.currentFontChanged.connect(self._save_appearance)
        self.font_size_spin.valueChanged.connect(self._save_appearance)
        self.font_weight_combo.currentIndexChanged.connect(self._save_appearance)
        self.density_combo.currentIndexChanged.connect(self._save_appearance)
        self.icon_size_spin.valueChanged.connect(self._save_appearance)
        self.panel_radius_spin.valueChanged.connect(self._save_appearance)
        self.reset_appearance_button.clicked.connect(self.reset_appearance)

    def _build_models_tab(self) -> None:
        layout = self._new_tab("模型与预设")
        presets = QGroupBox("装入参数预设")
        form = QFormLayout(presets)
        self.preset_combo = QComboBox()
        form.addRow("已有预设", self.preset_combo)
        self.preset_name_edit = QLineEdit()
        self.preset_name_edit.setMaxLength(100)
        form.addRow("预设名称", self.preset_name_edit)
        self.preset_model_combo = QComboBox()
        form.addRow("适用模型", self.preset_model_combo)
        self.preset_device_combo = QComboBox()
        for text, value in (("自动", "auto"), ("CPU", "cpu"), ("GPU", "gpu")):
            self.preset_device_combo.addItem(text, value)
        form.addRow("目标设备", self.preset_device_combo)
        self.preset_context_spin = QSpinBox()
        self.preset_context_spin.setRange(0, 1_048_576)
        self.preset_context_spin.setSpecialValueText("模型默认")
        form.addRow("上下文长度", self.preset_context_spin)
        self.preset_gpu_layers_spin = QSpinBox()
        self.preset_gpu_layers_spin.setRange(-1, 4096)
        self.preset_gpu_layers_spin.setSpecialValueText("全部可卸载层")
        form.addRow("GPU 层数", self.preset_gpu_layers_spin)
        self.preset_threads_spin = QSpinBox()
        self.preset_threads_spin.setRange(0, 256)
        self.preset_threads_spin.setSpecialValueText("未指定")
        self.preset_threads_spin.setToolTip("该字段供 llama.cpp 量化转换配置使用，不会伪传给不支持的装入接口。")
        form.addRow("转换线程数", self.preset_threads_spin)
        preset_buttons = QHBoxLayout()
        self.preset_save_button = QPushButton("保存 / 覆盖同名预设")
        self.preset_delete_button = QPushButton("删除所选预设")
        preset_buttons.addWidget(self.preset_save_button)
        preset_buttons.addWidget(self.preset_delete_button)
        form.addRow("操作", preset_buttons)
        layout.addWidget(presets)

        prefs = QGroupBox("备注、标签、收藏与置顶")
        form = QFormLayout(prefs)
        self.pref_model_combo = QComboBox()
        form.addRow("模型", self.pref_model_combo)
        self.pref_favorite_check = QCheckBox("收藏")
        self.pref_pinned_check = QCheckBox("置顶")
        flags = QWidget()
        flags_layout = QHBoxLayout(flags)
        flags_layout.setContentsMargins(0, 0, 0, 0)
        flags_layout.addWidget(self.pref_favorite_check)
        flags_layout.addWidget(self.pref_pinned_check)
        flags_layout.addStretch(1)
        form.addRow("列表标记", flags)
        self.pref_tags_edit = QLineEdit()
        self.pref_tags_edit.setPlaceholderText("多个标签用逗号分隔")
        form.addRow("标签", self.pref_tags_edit)
        self.pref_notes_edit = QPlainTextEdit()
        self.pref_notes_edit.setMaximumHeight(100)
        form.addRow("备注", self.pref_notes_edit)
        self.pref_save_button = QPushButton("保存模型偏好")
        form.addRow("操作", self.pref_save_button)
        layout.addWidget(prefs)
        layout.addStretch(1)

        self.preset_combo.currentIndexChanged.connect(self._select_preset)
        self.preset_save_button.clicked.connect(self.save_preset)
        self.preset_delete_button.clicked.connect(self.delete_preset)
        self.pref_model_combo.currentIndexChanged.connect(self._load_preference)
        self.pref_save_button.clicked.connect(self.save_preference)

    def _build_statistics_tab(self) -> None:
        layout = self._new_tab("统计与对比")
        stats_box = QGroupBox("使用统计与热度")
        stats_layout = QVBoxLayout(stats_box)
        row = QHBoxLayout()
        self.stats_days_combo = QComboBox()
        for days in (7, 30, 90):
            self.stats_days_combo.addItem(f"最近 {days} 天", days)
        self.stats_days_combo.setCurrentIndex(1)
        self.stats_refresh_button = QPushButton("刷新统计")
        row.addWidget(self.stats_days_combo)
        row.addWidget(self.stats_refresh_button)
        row.addStretch(1)
        stats_layout.addLayout(row)
        self.stats_summary_label = QLabel("尚无使用统计")
        self.stats_summary_label.setWordWrap(True)
        stats_layout.addWidget(self.stats_summary_label)
        self.stats_table = _table(("模型", "调用次数", "输入 Tokens", "输出 Tokens", "总耗时", "最近使用"))
        stats_layout.addWidget(self.stats_table)
        layout.addWidget(stats_box)

        compare_box = QGroupBox("两个模型只读对比")
        compare_layout = QVBoxLayout(compare_box)
        row = QHBoxLayout()
        self.compare_a_combo = QComboBox()
        self.compare_b_combo = QComboBox()
        self.compare_button = QPushButton("对比")
        row.addWidget(self.compare_a_combo, 1)
        row.addWidget(self.compare_b_combo, 1)
        row.addWidget(self.compare_button)
        compare_layout.addLayout(row)
        self.compare_table = _table(("项目", "模型 A", "模型 B"))
        compare_layout.addWidget(self.compare_table)
        layout.addWidget(compare_box)
        layout.addStretch(1)
        self.stats_refresh_button.clicked.connect(self.refresh_statistics)
        self.stats_days_combo.currentIndexChanged.connect(self.refresh_statistics)
        self.compare_button.clicked.connect(self.compare_models)

    def _build_data_tab(self) -> None:
        layout = self._new_tab("数据与日志")
        transfer = QGroupBox("清单与配置导入导出")
        transfer_layout = QVBoxLayout(transfer)
        hint = QLabel("JSON 仅作为数据读取；导入先预览，明确确认后才写 SQLite，不执行文件中的命令或自动扫描。")
        hint.setWordWrap(True)
        transfer_layout.addWidget(hint)
        buttons = QHBoxLayout()
        self.export_manifest_button = QPushButton("导出模型清单")
        self.import_manifest_button = QPushButton("预览并导入模型清单")
        self.export_config_button = QPushButton("导出配置")
        self.import_config_button = QPushButton("预览并导入配置")
        for button in (
            self.export_manifest_button, self.import_manifest_button,
            self.export_config_button, self.import_config_button,
        ):
            buttons.addWidget(button)
        transfer_layout.addLayout(buttons)
        catalog_row = QHBoxLayout()
        self.refresh_catalog_button = QPushButton("确认联网更新内置资源清单")
        self.refresh_catalog_button.setToolTip("访问官方 HTTPS API；失败保留内置或缓存清单，不会自动下载模型。")
        catalog_row.addWidget(self.refresh_catalog_button)
        catalog_row.addStretch(1)
        transfer_layout.addLayout(catalog_row)
        layout.addWidget(transfer)

        logs = QGroupBox("操作日志")
        logs_layout = QVBoxLayout(logs)
        row = QHBoxLayout()
        self.log_status_combo = QComboBox()
        for text, value in (
            ("全部状态", None), ("成功", "success"), ("失败", "error"),
            ("部分成功", "partial"), ("已阻止", "blocked"), ("预警", "warning"),
        ):
            self.log_status_combo.addItem(text, value)
        self.log_limit_spin = QSpinBox()
        self.log_limit_spin.setRange(10, 1000)
        self.log_limit_spin.setValue(100)
        self.log_refresh_button = QPushButton("刷新日志")
        row.addWidget(self.log_status_combo)
        row.addWidget(self.log_limit_spin)
        row.addWidget(self.log_refresh_button)
        row.addStretch(1)
        logs_layout.addLayout(row)
        self.log_table = _table(("时间", "操作", "目标", "状态", "消息"))
        logs_layout.addWidget(self.log_table)
        layout.addWidget(logs)
        layout.addStretch(1)

        self.export_manifest_button.clicked.connect(self.export_manifest)
        self.import_manifest_button.clicked.connect(self.import_manifest)
        self.export_config_button.clicked.connect(self.export_config)
        self.import_config_button.clicked.connect(self.import_config)
        self.refresh_catalog_button.clicked.connect(self.refresh_catalog)
        self.log_refresh_button.clicked.connect(self.refresh_logs)
        self.log_status_combo.currentIndexChanged.connect(self.refresh_logs)
        self.log_limit_spin.valueChanged.connect(self.refresh_logs)

    def _load_settings(self) -> None:
        values = self.settings.all()
        self._loading = True
        try:
            try:
                seconds = int(values.get("status_poll_interval_ms", 5000)) // 1000
            except (TypeError, ValueError, OverflowError):
                seconds = 5
            self.poll_seconds.setValue(max(1, min(3600, seconds)))
            self.minimized_slowdown_check.setChecked(bool(values.get("minimized_poll_slowdown", True)))
            self.warning_enabled_check.setChecked(bool(values.get("resource_warning_enabled", True)))
            try:
                threshold = int(values.get("resource_warning_percent", 90))
            except (TypeError, ValueError, OverflowError):
                threshold = 90
            self.warning_percent_spin.setValue(max(50, min(99, threshold)))
            self.warning_sound_check.setChecked(bool(values.get("resource_warning_sound", True)))
            self.theme_combo.setCurrentIndex(_combo_index(self.theme_combo, values.get("theme", "system")))
            self.accent_combo.setCurrentIndex(_combo_index(self.accent_combo, values.get("accent_color", "#4f8cff")))
            try:
                opacity = float(values.get("window_opacity", 1.0))
            except (TypeError, ValueError, OverflowError):
                opacity = 1.0
            if opacity <= 1.0:
                opacity *= 100
            self.opacity_slider.setValue(max(70, min(100, round(opacity))))
            self.opacity_label.setText(f"{self.opacity_slider.value()}%")
            self.font_family_combo.setCurrentFont(QFont(str(values.get("font_family", "Microsoft YaHei UI"))))
            try:
                font_size = int(values.get("font_size", 13))
            except (TypeError, ValueError, OverflowError):
                font_size = 13
            self.font_size_spin.setValue(max(9, min(24, font_size)))
            self.font_weight_combo.setCurrentIndex(_combo_index(self.font_weight_combo, values.get("font_weight", 400)))
            self.density_combo.setCurrentIndex(_combo_index(self.density_combo, values.get("density", "standard")))
            for widget, key, default, low, high in (
                (self.icon_size_spin, "icon_size", 18, 12, 48),
                (self.panel_radius_spin, "panel_radius", 6, 0, 20),
            ):
                try:
                    number = int(values.get(key, default))
                except (TypeError, ValueError, OverflowError):
                    number = default
                widget.setValue(max(low, min(high, number)))
        finally:
            self._loading = False

    def _save_runtime(self, *_args: Any) -> None:
        if self._loading:
            return
        self.settings.update({
            "status_poll_interval_ms": self.poll_seconds.value() * 1000,
            "minimized_poll_slowdown": self.minimized_slowdown_check.isChecked(),
            "resource_warning_enabled": self.warning_enabled_check.isChecked(),
            "resource_warning_percent": self.warning_percent_spin.value(),
            "resource_warning_sound": self.warning_sound_check.isChecked(),
        })
        self.settings_changed.emit(self.settings.all())

    def _save_appearance(self, *_args: Any) -> None:
        if self._loading:
            return
        self.opacity_label.setText(f"{self.opacity_slider.value()}%")
        self.settings.update({
            "theme": self.theme_combo.currentData(),
            "accent_color": self.accent_combo.currentData(),
            "window_opacity": self.opacity_slider.value() / 100.0,
            "font_family": self.font_family_combo.currentFont().family(),
            "font_size": self.font_size_spin.value(),
            "font_weight": self.font_weight_combo.currentData(),
            "density": self.density_combo.currentData(),
            "icon_size": self.icon_size_spin.value(),
            "panel_radius": self.panel_radius_spin.value(),
        })
        self.settings_changed.emit(self.settings.all())

    def reset_appearance(self) -> None:
        self.settings.update({key: Settings.DEFAULTS[key] for key in _APPEARANCE_KEYS})
        self._load_settings()
        self.settings_changed.emit(self.settings.all())
        self.status_message.emit("已恢复并实时应用默认外观。")

    @staticmethod
    def _model_name(model: Any) -> str:
        return f"{model.display_name or model.file_name} · {model.provider or 'unknown'}"

    def _fill_model_combo(self, combo: QComboBox, selected: Any = "", *, generic: bool = False) -> None:
        combo.blockSignals(True)
        combo.clear()
        if generic:
            combo.addItem("通用预设（不限模型）", "")
        for model in self._models:
            combo.addItem(self._model_name(model), model.model_id)
        combo.setCurrentIndex(_combo_index(combo, selected))
        combo.blockSignals(False)

    def reload_models(self) -> None:
        if self._shutting_down:
            return
        self._models = self.service.list_models()
        selected_preset = self.preset_model_combo.currentData()
        selected_pref = self.pref_model_combo.currentData()
        selected_a = self.compare_a_combo.currentData()
        selected_b = self.compare_b_combo.currentData()
        self._fill_model_combo(self.preset_model_combo, selected_preset, generic=True)
        self._fill_model_combo(self.pref_model_combo, selected_pref)
        self._fill_model_combo(self.compare_a_combo, selected_a)
        self._fill_model_combo(self.compare_b_combo, selected_b)
        if self.compare_b_combo.count() > 1 and self.compare_b_combo.currentIndex() == self.compare_a_combo.currentIndex():
            self.compare_b_combo.setCurrentIndex(1)
        self.pref_save_button.setEnabled(self.pref_model_combo.currentData() is not None)
        self.compare_button.setEnabled(bool(self._models))
        self._load_preference()

    def _preset_index(self, name: str) -> int:
        for index in range(self.preset_combo.count()):
            data = self.preset_combo.itemData(index)
            if isinstance(data, dict) and data.get("name") == name:
                return index
        return -1

    def refresh_presets(self) -> None:
        current = self.preset_combo.currentData()
        selected = str(current.get("name", "")) if isinstance(current, dict) else ""
        rows = self.store.connection.execute("SELECT * FROM load_presets ORDER BY name").fetchall()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("新建预设", None)
        for row in rows:
            self.preset_combo.addItem(str(row["name"]), dict(row))
        index = self._preset_index(selected)
        self.preset_combo.setCurrentIndex(index if index >= 0 else 0)
        self.preset_combo.blockSignals(False)
        self.preset_delete_button.setEnabled(self.preset_combo.currentData() is not None)
        self._select_preset()

    def _select_preset(self, *_args: Any) -> None:
        preset = self.preset_combo.currentData()
        self.preset_delete_button.setEnabled(isinstance(preset, dict))
        if not isinstance(preset, dict):
            return
        self.preset_name_edit.setText(str(preset.get("name", "")))
        self.preset_model_combo.setCurrentIndex(_combo_index(self.preset_model_combo, preset.get("model_id", "")))
        self.preset_device_combo.setCurrentIndex(_combo_index(self.preset_device_combo, preset.get("device", "auto")))
        for widget, key in (
            (self.preset_context_spin, "context_length"),
            (self.preset_gpu_layers_spin, "gpu_layers"),
            (self.preset_threads_spin, "threads"),
        ):
            try:
                value = int(preset.get(key, 0))
            except (TypeError, ValueError, OverflowError):
                value = 0
            widget.setValue(max(widget.minimum(), min(widget.maximum(), value)))

    def save_preset(self) -> None:
        name = self.preset_name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "预设名称不能为空", "请输入一个非空预设名称。")
            return
        model_id = str(self.preset_model_combo.currentData() or "")
        model = next((item for item in self._models if item.model_id == model_id), None)
        now = time.time()
        try:
            with self.store.transaction() as conn:
                conn.execute(
                    """INSERT INTO load_presets(
                           name, model_id, provider, device, context_length,
                           gpu_layers, threads, options, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                           model_id=excluded.model_id, provider=excluded.provider,
                           device=excluded.device, context_length=excluded.context_length,
                           gpu_layers=excluded.gpu_layers, threads=excluded.threads,
                           options=excluded.options, updated_at=excluded.updated_at""",
                    (
                        name, model_id, model.provider if model else "",
                        str(self.preset_device_combo.currentData()),
                        self.preset_context_spin.value(), self.preset_gpu_layers_spin.value(),
                        self.preset_threads_spin.value(), now, now,
                    ),
                )
            self.store.operations.add("preset.save", name, message="装入参数预设已保存；未执行装入。")
        except Exception as exc:
            self._show_error("保存预设失败", exc)
            return
        self.refresh_presets()
        index = self._preset_index(name)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)
        self.status_message.emit(f"预设“{name}”已保存，可在装入对话框选择并重新预检。")

    def delete_preset(self) -> None:
        preset = self.preset_combo.currentData()
        if not isinstance(preset, dict):
            return
        name = str(preset.get("name", ""))
        answer = QMessageBox.question(
            self, "删除参数预设", f"仅删除预设“{name}”，不会删除模型或装入参数以外的数据。确认？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            with self.store.transaction() as conn:
                conn.execute("DELETE FROM load_presets WHERE name=?", (name,))
            self.store.operations.add("preset.delete", name, message="参数预设已删除。")
        except Exception as exc:
            self._show_error("删除预设失败", exc)
            return
        self.refresh_presets()
        self.status_message.emit(f"预设“{name}”已删除。")

    def _load_preference(self, *_args: Any) -> None:
        model_id = self.pref_model_combo.currentData()
        if not isinstance(model_id, str) or not model_id:
            self.pref_favorite_check.setChecked(False)
            self.pref_pinned_check.setChecked(False)
            self.pref_tags_edit.clear()
            self.pref_notes_edit.clear()
            return
        prefs = self.store.prefs.get(model_id)
        self.pref_favorite_check.setChecked(prefs["favorite"])
        self.pref_pinned_check.setChecked(prefs["pinned"])
        self.pref_tags_edit.setText(", ".join(prefs["tags"]))
        self.pref_notes_edit.setPlainText(prefs["notes"])

    def save_preference(self) -> None:
        model_id = self.pref_model_combo.currentData()
        if not isinstance(model_id, str) or not model_id:
            return
        tags = list(dict.fromkeys(
            tag.strip() for tag in self.pref_tags_edit.text().replace("，", ",").split(",") if tag.strip()
        ))
        try:
            self.store.prefs.set(
                model_id,
                favorite=self.pref_favorite_check.isChecked(),
                pinned=self.pref_pinned_check.isChecked(),
                tags=tags,
                notes=self.pref_notes_edit.toPlainText(),
            )
            self.store.operations.add("model.prefs", model_id, message="模型偏好已保存。", details={"tags": tags})
        except Exception as exc:
            self._show_error("保存模型偏好失败", exc)
            return
        self.models_changed.emit()
        self.status_message.emit("模型备注、标签、收藏与置顶已保存。")

    def refresh_statistics(self, *_args: Any) -> None:
        try:
            days = int(self.stats_days_combo.currentData() or 30)
            result = self.service.usage_statistics(days=days)
        except Exception as exc:
            self._show_error("读取统计失败", exc)
            return
        summary = result["summary"]
        self.stats_summary_label.setText(
            f"最近 {days} 天：调用 {summary['event_count']} 次；输入 {summary['input_tokens']} Tokens；"
            f"输出 {summary['output_tokens']} Tokens；累计耗时 {summary['duration_ms'] / 1000:.1f} 秒。"
        )
        names = {model.model_id: model.display_name or model.file_name for model in self._models}
        aggregated: dict[str, dict[str, Any]] = {}
        for row in result["daily"]:
            item = aggregated.setdefault(row["model_id"], {
                "event_count": 0, "input_tokens": 0, "output_tokens": 0, "duration_ms": 0, "last": "",
            })
            for key in ("event_count", "input_tokens", "output_tokens", "duration_ms"):
                item[key] += int(row[key])
            item["last"] = max(item["last"], row["day"])
        rows = sorted(aggregated.items(), key=lambda pair: pair[1]["event_count"], reverse=True)
        self.stats_table.setRowCount(len(rows))
        for row_index, (model_id, values) in enumerate(rows):
            columns = (
                names.get(model_id, model_id), values["event_count"], values["input_tokens"],
                values["output_tokens"], f"{values['duration_ms'] / 1000:.1f} 秒", values["last"],
            )
            for column, value in enumerate(columns):
                self.stats_table.setItem(row_index, column, QTableWidgetItem(str(value)))

    def compare_models(self) -> None:
        first = self.compare_a_combo.currentData()
        second = self.compare_b_combo.currentData()
        if not first or not second or first == second:
            QMessageBox.warning(self, "请选择两个不同模型", "模型对比需要选择两个不同的已登记模型。")
            return
        try:
            result = self.service.compare_models([first, second])
        except Exception as exc:
            self._show_error("模型对比失败", exc)
            return
        models = result["models"]
        by_id = {item["model_id"]: item for item in models}
        first_model, second_model = by_id[first], by_id[second]
        fields = (
            ("display_name", "名称"), ("provider", "后端"), ("format", "格式"),
            ("quant", "量化"), ("size_bytes", "大小"), ("params_b", "参数量"),
            ("architecture", "架构"), ("context_length", "上下文"), ("integrity", "完整性"),
            ("file_path", "路径 / URI"),
        )
        self.compare_table.setRowCount(len(fields))
        for row, (key, label) in enumerate(fields):
            values = [first_model.get(key), second_model.get(key)]
            if key == "size_bytes":
                values = [human_size(int(value or 0)) for value in values]
            elif key == "params_b":
                values = [f"{float(value or 0):g} B" if value else "未知" for value in values]
            elif key == "context_length":
                values = [value or "未知" for value in values]
            difference = key in result["differences"]
            self.compare_table.setItem(row, 0, QTableWidgetItem(label + ("（差异）" if difference else "")))
            self.compare_table.setItem(row, 1, QTableWidgetItem(str(values[0] or "—")))
            self.compare_table.setItem(row, 2, QTableWidgetItem(str(values[1] or "—")))

    def _save_path(self, title: str, suggested: str) -> str:
        path, _ = QFileDialog.getSaveFileName(self, title, str(Path.home() / suggested), "JSON 文件 (*.json)")
        return path

    def export_manifest(self) -> None:
        path = self._save_path("导出模型清单", "模型清单.json")
        if not path:
            return
        try:
            result = self.service.export_models(path)
            self._show_result("导出模型清单", result)
        except Exception as exc:
            self._show_error("导出模型清单失败", exc)

    def export_config(self) -> None:
        path = self._save_path("导出配置", "本地模型管家配置.json")
        if not path:
            return
        try:
            result = self.service.export_config(path)
            self._show_result("导出配置", result)
        except Exception as exc:
            self._show_error("导出配置失败", exc)

    def _import_path(self, title: str) -> str:
        path, _ = QFileDialog.getOpenFileName(self, title, str(Path.home()), "JSON 文件 (*.json)")
        return path

    def import_manifest(self) -> None:
        path = self._import_path("选择模型清单")
        if path:
            self._import_preview(path, config=False)

    def import_config(self) -> None:
        path = self._import_path("选择配置")
        if path:
            self._import_preview(path, config=True)

    def _import_preview(self, path: str, *, config: bool) -> None:
        try:
            preview = (
                self.service.import_config(path, apply=False)
                if config else self.service.import_models(path, apply=False)
            )
            if config:
                summary = (
                    f"配置校验通过：设置 {len(preview.data.get('settings', {}))} 项，"
                    f"预设 {len(preview.data.get('load_presets', []))} 项，"
                    f"扫描根 {len(preview.data.get('scan_roots', []))} 项。"
                )
            else:
                summary = f"模型清单校验通过：{preview.data.get('model_count', 0)} 个模型。"
            answer = QMessageBox.question(
                self, "确认写入导入数据",
                summary + "\n\n当前仅完成预览。确认写入本机 SQLite？不会执行 JSON 中的命令、扫描或安装。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.status_message.emit("导入已取消；仅完成预览，未写入数据。")
                return
            result = (
                self.service.import_config(path, apply=True)
                if config else self.service.import_models(path, apply=True)
            )
        except Exception as exc:
            self._show_error("导入失败", exc)
            return
        if config:
            self._load_settings()
            self.settings_changed.emit(self.settings.all())
        else:
            self.reload_models()
            self.models_changed.emit()
        self.refresh_presets()
        self.refresh_logs()
        self._show_result("导入完成", result)

    def refresh_catalog(self) -> None:
        if self._shutting_down:
            return
        answer = QMessageBox.question(
            self, "确认联网更新资源清单",
            "此操作只访问官方 HTTPS API 并更新清单缓存；不会下载模型、执行安装或启动推理。确认联网？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        task = self.tasks.submit(_CATALOG_TASK_KEY, self.catalog.refresh, force=True)
        if task is None:
            self.status_message.emit("资源清单刷新已在运行，未重复提交。")
            return
        self.refresh_catalog_button.setEnabled(False)
        task.signals.result.connect(self._on_catalog_result)
        task.signals.error.connect(self._on_catalog_error)
        task.signals.finished.connect(self._on_catalog_finished)
        self.status_message.emit("已提交官方资源清单后台刷新。")

    def _on_catalog_result(self, result: Any) -> None:
        if self._shutting_down or not isinstance(result, CatalogSnapshot):
            return
        message = f"资源清单：{len(result.resources)} 项；来源 {result.source}。"
        if result.errors:
            message += " 问题：" + "；".join(result.errors)
        self.status_message.emit(message)
        self.refresh_logs()

    def _on_catalog_error(self, error: Any) -> None:
        if not self._shutting_down:
            self._show_error("资源清单刷新失败", error)

    def _on_catalog_finished(self) -> None:
        if not self._shutting_down:
            self.refresh_catalog_button.setEnabled(True)

    def refresh_logs(self, *_args: Any) -> None:
        try:
            rows = self.service.operation_logs(
                limit=self.log_limit_spin.value(), status=self.log_status_combo.currentData(),
            )
        except Exception as exc:
            self._show_error("读取日志失败", exc)
            return
        self.log_table.setRowCount(len(rows))
        for row_index, entry in enumerate(rows):
            created = datetime.fromtimestamp(entry["created_at"]).strftime("%Y-%m-%d %H:%M:%S")
            values = (
                created, entry["action"], entry["target"], entry["status"], entry["message"],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 4:
                    item.setToolTip(json.dumps(entry["details"], ensure_ascii=False, indent=2))
                self.log_table.setItem(row_index, column, item)

    def _show_result(self, title: str, result: OperationResult) -> None:
        self.status_message.emit(result.message)
        self.refresh_logs()
        QMessageBox.information(self, title, result.message)

    def _show_error(self, title: str, error: Any) -> None:
        message = str(error)
        self.status_message.emit(f"{title}：{message}")
        if not self._shutting_down:
            QMessageBox.critical(self, title, message)

    def shutdown(self) -> None:
        self._shutting_down = True
        self.tasks.cancel(_CATALOG_TASK_KEY)
