"""SQLite schema 版本与渐进迁移。"""
from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 1

_MODEL_COLUMN_UPGRADES = {
    "model_id": "TEXT NOT NULL DEFAULT ''",
    "display_name": "TEXT NOT NULL DEFAULT ''",
    "context_length": "INTEGER NOT NULL DEFAULT 0",
    "license": "TEXT NOT NULL DEFAULT ''",
    "integrity": "TEXT NOT NULL DEFAULT 'unknown'",
    "integrity_error": "TEXT NOT NULL DEFAULT ''",
    "source_type": "TEXT NOT NULL DEFAULT ''",
    "shard_index": "INTEGER NOT NULL DEFAULT 0",
    "shard_count": "INTEGER NOT NULL DEFAULT 1",
    "shard_files": "TEXT NOT NULL DEFAULT '[]'",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    file_path       TEXT PRIMARY KEY,
    file_name       TEXT NOT NULL,
    format          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    architecture    TEXT NOT NULL DEFAULT '',
    params_b        REAL NOT NULL DEFAULT 0,
    quant           TEXT NOT NULL DEFAULT '',
    source_dir      TEXT NOT NULL DEFAULT '',
    mtime           REAL NOT NULL DEFAULT 0,
    provider        TEXT NOT NULL DEFAULT '',
    model_ref       TEXT NOT NULL DEFAULT '',
    discovered_at   REAL NOT NULL DEFAULT 0,
    model_id        TEXT NOT NULL DEFAULT '',
    display_name    TEXT NOT NULL DEFAULT '',
    context_length  INTEGER NOT NULL DEFAULT 0,
    license         TEXT NOT NULL DEFAULT '',
    integrity       TEXT NOT NULL DEFAULT 'unknown',
    integrity_error TEXT NOT NULL DEFAULT '',
    source_type     TEXT NOT NULL DEFAULT '',
    shard_index     INTEGER NOT NULL DEFAULT 0,
    shard_count     INTEGER NOT NULL DEFAULT 1,
    shard_files     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS model_files (
    file_path       TEXT PRIMARY KEY,
    model_path      TEXT NOT NULL,
    file_name       TEXT NOT NULL DEFAULT '',
    shard_index     INTEGER NOT NULL DEFAULT 0,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    mtime           REAL NOT NULL DEFAULT 0,
    sha256          TEXT NOT NULL DEFAULT '',
    integrity       TEXT NOT NULL DEFAULT 'unknown',
    integrity_error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (model_path) REFERENCES models(file_path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_prefs (
    model_id    TEXT PRIMARY KEY,
    favorite    INTEGER NOT NULL DEFAULT 0,
    pinned      INTEGER NOT NULL DEFAULT 0,
    tags        TEXT NOT NULL DEFAULT '[]',
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_roots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL UNIQUE,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL,
    last_scanned_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id      INTEGER,
    started_at   REAL NOT NULL,
    finished_at  REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'running',
    models_found INTEGER NOT NULL DEFAULT 0,
    error        TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (root_id) REFERENCES scan_roots(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS hardware_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at REAL NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS load_presets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    model_id       TEXT NOT NULL DEFAULT '',
    provider       TEXT NOT NULL DEFAULT '',
    device         TEXT NOT NULL DEFAULT 'auto',
    context_length INTEGER NOT NULL DEFAULT 0,
    gpu_layers     INTEGER NOT NULL DEFAULT 0,
    threads        INTEGER NOT NULL DEFAULT 0,
    options        TEXT NOT NULL DEFAULT '{}',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id     TEXT NOT NULL,
    provider     TEXT NOT NULL DEFAULT '',
    started_at   REAL NOT NULL,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'success'
);

CREATE TABLE IF NOT EXISTS usage_daily (
    day           TEXT NOT NULL,
    model_id      TEXT NOT NULL,
    event_count   INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model_id)
);

CREATE TABLE IF NOT EXISTS resources (
    id           TEXT PRIMARY KEY,
    model_id     TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    source_url   TEXT NOT NULL DEFAULT '',
    license      TEXT NOT NULL DEFAULT '',
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    metadata     TEXT NOT NULL DEFAULT '{}',
    updated_at   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS download_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id      TEXT NOT NULL DEFAULT '',
    model_id         TEXT NOT NULL DEFAULT '',
    url              TEXT NOT NULL DEFAULT '',
    destination      TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    total_bytes      INTEGER NOT NULL DEFAULT 0,
    speed_bps        REAL NOT NULL DEFAULT 0,
    integrity        TEXT NOT NULL DEFAULT 'unknown',
    integrity_error  TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    started_at       REAL NOT NULL DEFAULT 0,
    finished_at      REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scenes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    config      TEXT NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS managed_processes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pid        INTEGER NOT NULL,
    provider   TEXT NOT NULL DEFAULT '',
    model_id   TEXT NOT NULL DEFAULT '',
    command    TEXT NOT NULL DEFAULT '[]',
    status     TEXT NOT NULL DEFAULT 'running',
    started_at REAL NOT NULL,
    stopped_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS operations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'success',
    message    TEXT NOT NULL DEFAULT '',
    details    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_models_provider ON models(provider);
CREATE INDEX IF NOT EXISTS idx_models_model_id ON models(model_id);
CREATE INDEX IF NOT EXISTS idx_model_files_model ON model_files(model_path);
CREATE INDEX IF NOT EXISTS idx_scan_runs_root ON scan_runs(root_id, started_at);
CREATE INDEX IF NOT EXISTS idx_hardware_captured ON hardware_snapshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_model ON usage_events(model_id, started_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_started ON usage_events(started_at);
CREATE INDEX IF NOT EXISTS idx_download_jobs_status ON download_jobs(status);
CREATE INDEX IF NOT EXISTS idx_managed_processes_status ON managed_processes(status);
CREATE INDEX IF NOT EXISTS idx_operations_created ON operations(created_at);
"""


def _upgrade_models_table(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(models)")}
    if not existing:
        return
    for name, definition in _MODEL_COLUMN_UPGRADES.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE models ADD COLUMN {name} {definition}")


def _migrate_1(conn: sqlite3.Connection) -> None:
    _upgrade_models_table(conn)
    conn.executescript(_SCHEMA)


_MIGRATIONS = {1: _migrate_1}


def migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"数据库版本 {version} 高于当前支持版本 {SCHEMA_VERSION}")
    for target in sorted(_MIGRATIONS):
        if version < target:
            _MIGRATIONS[target](conn)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
            version = target
