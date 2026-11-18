"""线程本地 SQLite 模型库：增量登记、范围清理与批量事务。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..core.config import DB_BUSY_TIMEOUT_MS, db_path
from ..core.models import ModelInfo
from .migrations import migrate

_COLUMNS = (
    "file_path", "file_name", "format", "size_bytes", "architecture",
    "params_b", "quant", "source_dir", "mtime", "provider",
    "model_ref", "discovered_at", "model_id", "display_name",
    "context_length", "license", "integrity", "integrity_error",
    "source_type", "shard_index", "shard_count", "shard_files",
)
_UPDATE_COLUMNS = tuple(
    column for column in _COLUMNS if column not in {"file_path", "discovered_at"}
)
_UPSERT_SQL = f"""
    INSERT INTO models ({', '.join(_COLUMNS)})
    VALUES ({', '.join('?' * len(_COLUMNS))})
    ON CONFLICT(file_path) DO UPDATE SET
    {', '.join(f'{column}=excluded.{column}' for column in _UPDATE_COLUMNS)},
    discovered_at=CASE
        WHEN models.discovered_at>0 THEN models.discovered_at
        ELSE excluded.discovered_at
    END
"""


def _normalise_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _inside_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


class ModelStore:
    def __init__(self, path: str | Path | None = None):
        self._path = str(path or db_path())
        self._uri = self._path == ":memory:"
        self._target = (
            f"file:model-store-{id(self)}?mode=memory&cache=shared"
            if self._uri else str(Path(self._path).expanduser())
        )
        if not self._uri:
            Path(self._target).parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._lock = threading.RLock()
        self._closed = False
        migrate(self.connection)

        from .repositories import (
            HardwareRepository,
            ModelPrefsRepository,
            OperationRepository,
            ScanRootRepository,
            SettingsRepository,
            UsageRepository,
        )
        self.settings = SettingsRepository(self)
        self.prefs = ModelPrefsRepository(self)
        self.operations = OperationRepository(self)
        self.usage = UsageRepository(self)
        self.scan_roots = ScanRootRepository(self)
        self.hardware = HardwareRepository(self)

    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            return conn
        with self._lock:
            if self._closed:
                raise RuntimeError("ModelStore 已关闭")
            conn = sqlite3.connect(
                self._target,
                uri=self._uri,
                timeout=DB_BUSY_TIMEOUT_MS / 1000,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._connections.add(conn)
            self._local.connection = conn
            return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    @staticmethod
    def _values(model: ModelInfo) -> tuple[Any, ...]:
        if not model.discovered_at:
            model.discovered_at = time.time()
        values = []
        for column in _COLUMNS:
            value = getattr(model, column)
            if column == "shard_files":
                value = json.dumps(value, ensure_ascii=False)
            values.append(value)
        return tuple(values)

    def upsert(self, m: ModelInfo) -> None:
        with self.transaction() as conn:
            conn.execute(_UPSERT_SQL, self._values(m))

    def upsert_many(self, items: Iterable[ModelInfo]) -> None:
        models = list(items)
        if not models:
            return
        with self.transaction() as conn:
            conn.executemany(_UPSERT_SQL, [self._values(model) for model in models])

    def all(self) -> list[ModelInfo]:
        rows = self.connection.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM models ORDER BY file_name COLLATE NOCASE"
        ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def delete(self, file_path: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM models WHERE file_path=?", (file_path,))

    def prune_missing(
        self,
        alive_paths: set[str],
        root: str | Path | None = None,
    ) -> int:
        """仅清理本次扫描范围内已消失的文件类记录，返回删除数。"""
        alive = {_normalise_path(path) for path in alive_paths}
        scope = _normalise_path(root) if root is not None else None
        rows = self.connection.execute(
            "SELECT file_path FROM models WHERE provider='local-file'"
        ).fetchall()
        missing = []
        for row in rows:
            candidate = _normalise_path(row["file_path"])
            if scope is not None and not _inside_root(candidate, scope):
                continue
            if candidate not in alive:
                missing.append((row["file_path"],))
        if missing:
            with self.transaction() as conn:
                conn.executemany("DELETE FROM models WHERE file_path=?", missing)
        return len(missing)

    def clear(self) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM models")

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> ModelInfo:
        values = {column: row[column] for column in _COLUMNS}
        try:
            values["shard_files"] = json.loads(values["shard_files"] or "[]")
        except (TypeError, ValueError):
            values["shard_files"] = []
        return ModelInfo(**values)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = tuple(self._connections)
            self._connections.clear()
        for conn in connections:
            conn.close()

    def __enter__(self) -> ModelStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
