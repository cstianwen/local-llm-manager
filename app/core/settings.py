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
        "theme": "system",
        "resource_warning_enabled": True,
        "resource_warning_percent": 90,
    }

    def __init__(self, store: ModelStore):
        self._repository = store.settings

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
