"""左侧栏底部的全局后台任务状态区。"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QFrame, QLabel, QProgressBar, QVBoxLayout, QWidget

_STATE_COLOR = {
    "running": "#8dbdff",
    "ok": "#63d68b",
    "error": "#ff8585",
    "cancelled": "#9aa7b8",
    "idle": "#8a97a9",
}
_STATE_PREFIX = {"running": "● ", "ok": "✓ ", "error": "✕ ", "cancelled": "— ", "idle": ""}


class TaskStatusWidget(QFrame):
    """所有后台任务的唯一进度与结论出口，常驻侧栏底部。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("taskStatus")
        self._state = "idle"
        self._text = "就绪"
        self._percent = -1
        self._started_at: float | None = None
        self._compact = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 2)
        layout.setSpacing(3)
        self.title_label = QLabel("就绪")
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-size: 11px; color: #8a97a9;")
        layout.addWidget(self.title_label)
        self.progress = QProgressBar()
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("font-size: 11px; color: #b9c5d6;")
        self.detail_label.setVisible(False)
        layout.addWidget(self.detail_label)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    def update_task(self, text: str, percent: int = -1, state: str = "running") -> None:
        """state: running / ok / error / cancelled。"""
        state = state if state in _STATE_COLOR else "running"
        running = state == "running"
        if running and self._state != "running":
            self._started_at = time.monotonic()
            self._timer.start()
        if not running:
            self._timer.stop()
            self._started_at = None
        self._state = state
        self._text = str(text or "后台任务进行中")
        self._percent = int(percent) if isinstance(percent, int) else -1
        self._render()

    def set_compact(self, compact: bool) -> None:
        self._compact = bool(compact)
        self.title_label.setVisible(not self._compact)
        self.detail_label.setVisible(not self._compact and bool(self.detail_label.text()))
        self._render()

    def _elapsed_text(self) -> str:
        if self._started_at is None:
            return ""
        return f" · 已用 {int(time.monotonic() - self._started_at)} 秒"

    def _tick(self) -> None:
        if self._state == "running":
            self._render()

    def _render(self) -> None:
        prefix = _STATE_PREFIX.get(self._state, "")
        text = f"{prefix}{self._text}{self._elapsed_text() if self._state == 'running' else ''}"
        color = _STATE_COLOR.get(self._state, "#8a97a9")
        self.setToolTip(text)
        if self._compact:
            self.title_label.setText(text)
            self.title_label.setToolTip(text)
        else:
            self.title_label.setText(text)
            self.title_label.setStyleSheet(f"font-size: 11px; color: {color};")
        self.detail_label.setVisible(not self._compact and bool(self.detail_label.text()))
        if self._state == "running":
            self.progress.setVisible(True)
            if self._percent >= 0:
                self.progress.setRange(0, 100)
                self.progress.setValue(min(100, self._percent))
            else:
                self.progress.setRange(0, 0)
        else:
            self.progress.setVisible(False)
            self.progress.setRange(0, 1)
            self.progress.setValue(0)

    def set_detail(self, text: str) -> None:
        self.detail_label.setText(str(text or ""))
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
