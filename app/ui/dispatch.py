"""把后台线程回调安全地排队到 GUI 线程执行。"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot


class GuiDispatch(QObject):
    """worker 只调用 ``post``，真正的 UI 操作在主线程事件循环中执行。"""

    _posted = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._posted.connect(self._invoke, Qt.ConnectionType.QueuedConnection)

    def post(self, callback: Callable[[], None]) -> None:
        self._posted.emit(callback)

    @Slot(object)
    def _invoke(self, callback: Callable[[], None]) -> None:
        try:
            callback()
        except RuntimeError:
            # 窗口已销毁时丢弃迟到的回调，不让异常穿回事件循环。
            pass
