"""基于 SQLite settings 表的轻量持久设置。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config import STATUS_POLL_INTERVAL_MS

if TYPE_CHECKING:
    from ..store.db import ModelStore


_MISSING = object()


class Settings:
    DEFAULTS: dict[str, Any] = {
        "status_poll_interval_ms": STATUS_POLL_INTERVAL_MS,
        "scan_interval_minutes": 60,
        "theme": "dark",
        "accent_color": "#4f8cff",
        "window_opacity": 1.0,
        "font_family": "Microsoft YaHei UI",
        "font_size": 13,
        "font_weight": 400,
        "density": "standard",
        "icon_size": 18,
        "panel_radius": 6,
        "minimized_poll_slowdown": True,
        "resource_warning_enabled": True,
        "resource_warning_sound": True,
        "resource_warning_percent": 90,
        "scan_scope_selection": [],
        "scan_default_roots": [],
    }

    # 旧版本界面把强调色改成了青绿，与设计稿的蓝色不一致；仅校正这一个历史值。
    LEGACY_ACCENT = "#159da8"
    DESIGN_ACCENT = "#4f8cff"

    def __init__(self, store: ModelStore):
        self._repository = store.settings
        if self._repository.get("accent_color") == self.LEGACY_ACCENT:
            self._repository.set("accent_color", self.DESIGN_ACCENT)

    def get(self, key: str, default: Any = _MISSING) -> Any:
        fallback = self.DEFAULTS.get(key) if default is _MISSING else default
        return self._repository.get(key, fallback)

    def set(self, key: str, value: Any) -> None:
        self._repository.set(key, value)

    def update(self, values: dict[str, Any]) -> None:
        self._repository.update(values)

    def all(self) -> dict[str, Any]:
        values = dict(self.DEFAULTS)
        values.update(self._repository.all())
        return values

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._repository.clear()
        else:
            self._repository.delete(key)
