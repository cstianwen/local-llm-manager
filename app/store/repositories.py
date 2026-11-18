"""面向 UI 与服务的 SQLite 实用仓储。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .db import ModelStore


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _normalise_root(path: str | Path) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(path)))


class SettingsRepository:
    def __init__(self, store: ModelStore):
        self._store = store

    def get(self, key: str, default: Any = None) -> Any:
        row = self._store.connection.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return default if row is None else _load(row["value"], default)

    def set(self, key: str, value: Any) -> None:
        self.update({key: value})

    def update(self, values: dict[str, Any]) -> None:
        if not values:
            return
        now = time.time()
        sql = """INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                 ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at"""
        with self._store.transaction() as conn:
            conn.executemany(sql, [(key, _dump(value), now) for key, value in values.items()])

    def all(self) -> dict[str, Any]:
        rows = self._store.connection.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: _load(row["value"], None) for row in rows}

    def delete(self, key: str) -> None:
        with self._store.transaction() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))

    def clear(self) -> None:
        with self._store.transaction() as conn:
            conn.execute("DELETE FROM settings")


class ModelPrefsRepository:
    _FIELDS = {"favorite", "pinned", "tags", "notes"}

    def __init__(self, store: ModelStore):
        self._store = store

    @staticmethod
    def _defaults(model_id: str) -> dict[str, Any]:
        return {
            "model_id": model_id, "favorite": False, "pinned": False,
            "tags": [], "notes": "", "updated_at": 0.0,
        }

    @staticmethod
    def _from_row(row: Any) -> dict[str, Any]:
        return {
            "model_id": row["model_id"],
            "favorite": bool(row["favorite"]),
            "pinned": bool(row["pinned"]),
            "tags": _load(row["tags"], []),
            "notes": row["notes"],
            "updated_at": row["updated_at"],
        }

    def get(self, model_id: str) -> dict[str, Any]:
        row = self._store.connection.execute(
            "SELECT * FROM model_prefs WHERE model_id=?", (model_id,)
        ).fetchone()
        return self._defaults(model_id) if row is None else self._from_row(row)

    def set(self, model_id: str, **changes: Any) -> dict[str, Any]:
        unknown = set(changes) - self._FIELDS
        if unknown:
            raise ValueError(f"未知模型偏好字段: {', '.join(sorted(unknown))}")
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM model_prefs WHERE model_id=?", (model_id,)
            ).fetchone()
            values = self._defaults(model_id) if row is None else self._from_row(row)
            values.update(changes)
            values["favorite"] = bool(values["favorite"])
            values["pinned"] = bool(values["pinned"])
            values["updated_at"] = time.time()
            conn.execute(
                """INSERT INTO model_prefs(
                       model_id, favorite, pinned, tags, notes, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(model_id) DO UPDATE SET
                       favorite=excluded.favorite, pinned=excluded.pinned,
                       tags=excluded.tags, notes=excluded.notes,
                       updated_at=excluded.updated_at""",
                (
                    model_id, values["favorite"], values["pinned"],
                    _dump(values["tags"]), values["notes"], values["updated_at"],
                ),
            )
        return values

    def set_favorite(self, model_id: str, favorite: bool = True) -> dict[str, Any]:
        return self.set(model_id, favorite=favorite)

    def favorites(self) -> list[dict[str, Any]]:
        rows = self._store.connection.execute(
            "SELECT * FROM model_prefs WHERE favorite=1 ORDER BY updated_at DESC"
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def delete(self, model_id: str) -> None:
        with self._store.transaction() as conn:
            conn.execute("DELETE FROM model_prefs WHERE model_id=?", (model_id,))


class OperationRepository:
    def __init__(self, store: ModelStore):
        self._store = store

    def add(
        self,
        action: str,
        target: str = "",
        status: str = "success",
        message: str = "",
        details: dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> int:
        with self._store.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO operations(action, target, status, message, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    action, target, status, message, _dump(details or {}),
                    time.time() if created_at is None else created_at,
                ),
            )
            return int(cursor.lastrowid)

    def recent(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM operations"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self._store.connection.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"], "action": row["action"], "target": row["target"],
                "status": row["status"], "message": row["message"],
                "details": _load(row["details"], {}), "created_at": row["created_at"],
            }
            for row in rows
        ]


class UsageRepository:
    def __init__(self, store: ModelStore):
        self._store = store

    def record(
        self,
        model_id: str,
        provider: str = "",
        duration_ms: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        status: str = "success",
        started_at: float | None = None,
    ) -> int:
        timestamp = time.time() if started_at is None else started_at
        day = time.strftime("%Y-%m-%d", time.localtime(timestamp))
        with self._store.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO usage_events(
                       model_id, provider, started_at, duration_ms,
                       input_tokens, output_tokens, status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    model_id, provider, timestamp, duration_ms,
                    input_tokens, output_tokens, status,
                ),
            )
            conn.execute(
                """INSERT INTO usage_daily(
                       day, model_id, event_count, input_tokens, output_tokens, duration_ms
                   ) VALUES (?, ?, 1, ?, ?, ?)
                   ON CONFLICT(day, model_id) DO UPDATE SET
                       event_count=usage_daily.event_count+1,
                       input_tokens=usage_daily.input_tokens+excluded.input_tokens,
                       output_tokens=usage_daily.output_tokens+excluded.output_tokens,
                       duration_ms=usage_daily.duration_ms+excluded.duration_ms""",
                (day, model_id, input_tokens, output_tokens, duration_ms),
            )
            return int(cursor.lastrowid)

    def daily(self, days: int = 30, model_id: str | None = None) -> list[dict[str, Any]]:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
        sql = "SELECT * FROM usage_daily WHERE day>=?"
        params: list[Any] = [cutoff]
        if model_id is not None:
            sql += " AND model_id=?"
            params.append(model_id)
        sql += " ORDER BY day, model_id"
        rows = self._store.connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def summary(self, days: int = 30) -> dict[str, int]:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
        row = self._store.connection.execute(
            """SELECT COALESCE(SUM(event_count), 0) AS event_count,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(duration_ms), 0) AS duration_ms
               FROM usage_daily WHERE day>=?""",
            (cutoff,),
        ).fetchone()
        return {key: int(row[key]) for key in row.keys()}


class ScanRootRepository:
    def __init__(self, store: ModelStore):
        self._store = store

    def add(self, path: str | Path, enabled: bool = True) -> int:
        root = _normalise_root(path)
        now = time.time()
        with self._store.transaction() as conn:
            conn.execute(
                """INSERT INTO scan_roots(path, enabled, created_at) VALUES (?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET enabled=excluded.enabled""",
                (root, enabled, now),
            )
            row = conn.execute("SELECT id FROM scan_roots WHERE path=?", (root,)).fetchone()
            return int(row["id"])

    def remove(self, path: str | Path) -> None:
        with self._store.transaction() as conn:
            conn.execute("DELETE FROM scan_roots WHERE path=?", (_normalise_root(path),))

    def set_enabled(self, path: str | Path, enabled: bool) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE scan_roots SET enabled=? WHERE path=?",
                (enabled, _normalise_root(path)),
            )

    def touch(self, path: str | Path, scanned_at: float | None = None) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE scan_roots SET last_scanned_at=? WHERE path=?",
                (time.time() if scanned_at is None else scanned_at, _normalise_root(path)),
            )

    def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scan_roots"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY path COLLATE NOCASE"
        rows = self._store.connection.execute(sql).fetchall()
        return [
            {
                "id": row["id"], "path": row["path"], "enabled": bool(row["enabled"]),
                "created_at": row["created_at"], "last_scanned_at": row["last_scanned_at"],
            }
            for row in rows
        ]


class HardwareRepository:
    def __init__(self, store: ModelStore):
        self._store = store

    @staticmethod
    def _from_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"], "captured_at": row["captured_at"],
            "data": _load(row["payload"], {}),
        }

    def save(self, snapshot: dict[str, Any], captured_at: float | None = None) -> int:
        with self._store.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO hardware_snapshots(captured_at, payload) VALUES (?, ?)",
                (time.time() if captured_at is None else captured_at, _dump(snapshot)),
            )
            return int(cursor.lastrowid)

    def latest(self) -> dict[str, Any] | None:
        row = self._store.connection.execute(
            "SELECT * FROM hardware_snapshots ORDER BY captured_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self._from_row(row)

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._store.connection.execute(
            "SELECT * FROM hardware_snapshots ORDER BY captured_at DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [self._from_row(row) for row in rows]
