"""无 Qt 取消令牌及基于现有 settings 仓储的会话持久化。"""
from __future__ import annotations

import copy
import threading
from typing import Any

from ..core.errors import OperationCancelledError
from ..core.settings import Settings
from .models import AgentSession


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def is_cancelled(self) -> bool:
        return self.cancelled

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise OperationCancelledError("Agent 回复已取消")


class SessionRepository:
    INDEX_KEY = "agent.sessions.index.v1"
    PREFIX = "agent.session.v1."

    def __init__(self, store: Any = None, settings: Any = None):
        if settings is None and store is not None:
            settings = Settings(store)
        self._settings = settings
        self._values: dict[str, Any] = {}
        self._lock = threading.RLock()

    def _get(self, key: str, default: Any) -> Any:
        if self._settings is not None:
            return self._settings.get(key, default)
        with self._lock:
            return copy.deepcopy(self._values.get(key, default))

    def _set(self, key: str, value: Any) -> None:
        if self._settings is not None:
            self._settings.set(key, value)
            return
        with self._lock:
            self._values[key] = copy.deepcopy(value)

    def _delete(self, key: str) -> None:
        if self._settings is not None:
            delete = getattr(self._settings, "delete", None)
            if callable(delete):
                delete(key)
            else:
                reset = getattr(self._settings, "reset", None)
                if callable(reset):
                    reset(key)
                else:
                    self._settings.set(key, None)
            return
        with self._lock:
            self._values.pop(key, None)

    def save(self, session: AgentSession) -> AgentSession:
        payload = session.to_dict()
        self._set(self.PREFIX + session.id, payload)
        index = self._get(self.INDEX_KEY, [])
        ids = [str(item) for item in index] if isinstance(index, list) else []
        ids = [session.id] + [item for item in ids if item != session.id]
        self._set(self.INDEX_KEY, ids[:500])
        return session

    def get(self, session_id: str) -> AgentSession | None:
        data = self._get(self.PREFIX + session_id, None)
        if not isinstance(data, dict):
            return None
        try:
            return AgentSession.from_dict(data)
        except (TypeError, ValueError, KeyError):
            return None

    def require(self, session_id: str) -> AgentSession:
        session = self.get(session_id)
        if session is None:
            raise KeyError(f"会话不存在或数据无效：{session_id}")
        return session

    def list(self, limit: int = 100) -> list[AgentSession]:
        index = self._get(self.INDEX_KEY, [])
        if not isinstance(index, list):
            return []
        result = []
        for session_id in index[:max(0, int(limit))]:
            if isinstance(session_id, str) and (session := self.get(session_id)) is not None:
                result.append(session)
        return result

    def delete(self, session_id: str) -> bool:
        existed = self.get(session_id) is not None
        self._delete(self.PREFIX + session_id)
        index = self._get(self.INDEX_KEY, [])
        if isinstance(index, list):
            self._set(self.INDEX_KEY, [item for item in index if item != session_id])
        return existed
