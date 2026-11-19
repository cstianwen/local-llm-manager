"""应用级主题、字体、密度与顶层窗口透明度。"""
from __future__ import annotations

import re
from string import Template
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow


_DARK = {
    "window": "#151922", "main": "#11151d", "sidebar": "#0d1118",
    "text": "#e7eaf0", "muted": "#a8b4c6", "button": "#242c3a",
    "button_hover": "#303b4e", "button_pressed": "#1c2532",
    "field": "#1b222e", "border": "#354157", "header": "#202938",
    "grid": "#293345", "disabled": "#727d8e", "disabled_bg": "#1b212c",
    "tooltip": "#222b39", "scroll": "#141a24", "scroll_handle": "#3a465a",
}
_LIGHT = {
    "window": "#f4f6f9", "main": "#edf0f5", "sidebar": "#e2e7ee",
    "text": "#202634", "muted": "#536176", "button": "#ffffff",
    "button_hover": "#e7eef8", "button_pressed": "#d5e0ee",
    "field": "#ffffff", "border": "#b8c3d2", "header": "#e4e9f0",
    "grid": "#d0d7e1", "disabled": "#8993a2", "disabled_bg": "#e4e7eb",
    "tooltip": "#ffffff", "scroll": "#e5e9ef", "scroll_handle": "#a5b0bf",
}

_STYLE = Template("""
QWidget {
    background: $window;
    color: $text;
    font-family: "$font_family", "Segoe UI", sans-serif;
    font-size: ${font_size}px;
    font-weight: $font_weight;
}
QMainWindow, QDialog { background: $main; }
QFrame#sidebar { background: $sidebar; border-right: 1px solid $border; }
QPushButton, QToolButton {
    background: $button;
    border: 1px solid $border;
    border-radius: $radiuspx;
    padding: ${button_padding}px 11px;
    min-height: ${control_height}px;
}
QPushButton:hover, QToolButton:hover { background: $button_hover; border-color: $accent; }
QPushButton:pressed, QToolButton:pressed { background: $button_pressed; }
QPushButton:disabled, QToolButton:disabled { color: $disabled; background: $disabled_bg; }
QToolButton { icon-size: ${icon_size}px; }
QToolButton#navButton {
    text-align: left; border: none; border-radius: $radiuspx;
    padding: ${nav_padding}px 12px; background: transparent;
}
QToolButton#navButton:checked { background: $accent_soft; color: $accent_text; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit,
QTextBrowser, QTableWidget, QListWidget, QTreeWidget {
    background: $field; color: $text; border: 1px solid $border;
    border-radius: $radiuspx; selection-background-color: $accent;
    selection-color: #ffffff;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    padding: ${field_padding}px 7px; min-height: ${control_height}px;
}
QComboBox QAbstractItemView { background: $field; selection-background-color: $accent; }
QGroupBox {
    border: 1px solid $border; border-radius: $radiuspx;
    margin-top: 12px; padding-top: 10px; font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: $muted; }
QLabel, QCheckBox, QRadioButton { background: transparent; }
QHeaderView::section {
    background: $header; color: $text; border: none;
    border-right: 1px solid $border; border-bottom: 1px solid $border;
    padding: ${header_padding}px;
}
QTableWidget { gridline-color: $grid; alternate-background-color: $header; }
QTableWidget::item { padding: ${item_padding}px; }
QTableWidget::item:selected { background: $accent_soft; }
QMenu { background: $field; border: 1px solid $border; }
QMenu::item { padding: 7px 26px; }
QMenu::item:selected { background: $accent; }
QStatusBar { background: $sidebar; border-top: 1px solid $border; }
QProgressBar {
    background: $field; border: 1px solid $border;
    border-radius: $radiuspx; text-align: center;
}
QProgressBar::chunk { background: $accent; }
QTabWidget::pane { border: 1px solid $border; border-radius: $radiuspx; }
QTabBar::tab {
    background: $button; border: 1px solid $border;
    padding: ${field_padding}px 13px; margin: 3px 2px 0 0;
    border-top-left-radius: $radiuspx; border-top-right-radius: $radiuspx;
}
QTabBar::tab:selected { background: $accent_soft; color: $accent_text; }
QScrollBar:vertical { background: $scroll; width: 12px; }
QScrollBar::handle:vertical { background: $scroll_handle; border-radius: 5px; min-height: 24px; }
QScrollBar:horizontal { background: $scroll; height: 12px; }
QScrollBar::handle:horizontal { background: $scroll_handle; border-radius: 5px; min-width: 24px; }
QToolTip { background: $tooltip; color: $text; border: 1px solid $border; padding: 4px; }
""")


def _integer(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _font_family(requested: Any) -> str:
    candidate = str(requested or "Microsoft YaHei UI").strip()
    if QApplication.instance() is None:
        return candidate.replace('"', "").replace("\n", " ").replace("\r", " ") or "Segoe UI"
    default = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    families = {name.casefold(): name for name in QFontDatabase.families()}
    return families.get(candidate.casefold(), default)


def _opacity(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    if number > 1.0:
        number /= 100.0
    return max(0.7, min(1.0, number))


def _resolved_dark(app: QApplication, mode: str) -> bool:
    if mode == "dark":
        return True
    if mode == "light":
        return False
    hints = app.styleHints()
    try:
        return hints.colorScheme() != Qt.ColorScheme.Light
    except (AttributeError, RuntimeError):
        return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def _palette(colors: dict[str, str], accent: str) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: colors["main"],
        QPalette.ColorRole.WindowText: colors["text"],
        QPalette.ColorRole.Base: colors["field"],
        QPalette.ColorRole.AlternateBase: colors["header"],
        QPalette.ColorRole.ToolTipBase: colors["tooltip"],
        QPalette.ColorRole.ToolTipText: colors["text"],
        QPalette.ColorRole.Text: colors["text"],
        QPalette.ColorRole.Button: colors["button"],
        QPalette.ColorRole.ButtonText: colors["text"],
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Link: accent,
        QPalette.ColorRole.Highlight: accent,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.PlaceholderText: colors["muted"],
    }
    for role, value in roles.items():
        palette.setColor(role, QColor(value))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(colors["disabled"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(colors["disabled"]))
    return palette


def _stylesheet(values: dict[str, Any], colors: dict[str, str], accent: str) -> str:
    density = str(values.get("density", "standard"))
    paddings = {
        "compact": (3, 3, 2, 4, 1, 16),
        "comfortable": (8, 8, 7, 9, 5, 24),
        "standard": (6, 5, 5, 7, 3, 20),
    }
    button, field, item, header, nav, height = paddings.get(density, paddings["standard"])
    dark = colors is _DARK
    accent_color = QColor(accent)
    substitutions = dict(colors)
    substitutions.update({
        "font_family": _font_family(values.get("font_family")),
        "font_size": _integer(values.get("font_size"), 13, 9, 24),
        "font_weight": _integer(values.get("font_weight"), 400, 100, 900),
        "radiuspx": f"{_integer(values.get('panel_radius'), 6, 0, 20)}px",
        "icon_size": _integer(values.get("icon_size"), 18, 12, 48),
        "button_padding": button, "field_padding": field, "item_padding": item,
        "header_padding": header, "nav_padding": nav, "control_height": height,
        "accent": accent,
        "accent_soft": accent_color.darker(175).name() if dark else accent_color.lighter(180).name(),
        "accent_text": accent_color.lighter(165).name() if dark else accent_color.darker(125).name(),
    })
    return _STYLE.substitute(substitutions)


DARK_STYLESHEET = _stylesheet({}, _DARK, "#4f8cff")


class _WindowOpacityFilter(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(watched, (QMainWindow, QDialog)):
            app = QApplication.instance()
            if app is not None:
                watched.setWindowOpacity(float(getattr(app, "_llm_window_opacity", 1.0)))
        return False


def _sync_window_opacity(app: QApplication, opacity: float) -> None:
    app._llm_window_opacity = opacity
    if not hasattr(app, "_llm_opacity_filter"):
        filter_object = _WindowOpacityFilter(app)
        app.installEventFilter(filter_object)
        app._llm_opacity_filter = filter_object
    for window in app.topLevelWidgets():
        if isinstance(window, (QMainWindow, QDialog)):
            window.setWindowOpacity(opacity)


def apply_theme(app: QApplication, values: dict[str, Any] | None = None) -> None:
    """应用并实时预览设置；调用只改变外观，不启动任何后端。"""
    values = dict(values or {})
    app._llm_theme_values = values
    mode = str(values.get("theme", "system"))
    if mode not in {"system", "dark", "light"}:
        mode = "system"
    colors = _DARK if _resolved_dark(app, mode) else _LIGHT
    requested_accent = str(values.get("accent_color", "#4f8cff"))
    accent = requested_accent if re.fullmatch(r"#[0-9a-fA-F]{6}", requested_accent) else "#4f8cff"
    family = _font_family(values.get("font_family"))
    size = _integer(values.get("font_size"), 13, 9, 24)
    weight = _integer(values.get("font_weight"), 400, 100, 900)
    app.setStyle("Fusion")
    app.setFont(QFont(family, size, weight))
    app.setPalette(_palette(colors, accent))
    app.setStyleSheet(_stylesheet(values, colors, accent))
    _sync_window_opacity(app, _opacity(values.get("window_opacity", 1.0)))

    hints = app.styleHints()
    scheme_changed = getattr(hints, "colorSchemeChanged", None)
    if scheme_changed is not None and not hasattr(app, "_llm_scheme_connection"):
        callback = lambda *_args: apply_theme(app, getattr(app, "_llm_theme_values", {}))
        app._llm_scheme_connection = scheme_changed.connect(callback)
        app._llm_scheme_callback = callback


def apply_dark_theme(app: QApplication) -> None:
    """兼容启动入口：应用默认深色主题。"""
    apply_theme(app, {"theme": "dark"})
