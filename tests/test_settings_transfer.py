import json

from app.core.settings import Settings
from app.services.transfer import CONFIG_SCHEMA, MODEL_SCHEMA, TRANSFER_VERSION, TransferService
from app.store.db import ModelStore


def test_settings_roundtrip_preserves_json_types(tmp_path):
    path = tmp_path / "settings.db"
    store = ModelStore(path)
    try:
        Settings(store).update({
            "theme": "light",
            "window_opacity": 0.85,
            "resource_warning_enabled": False,
            "model_table_column_widths": [120, 80],
        })
    finally:
        store.close()
    reopened = ModelStore(path)
    try:
        settings = Settings(reopened)
        assert settings.get("theme") == "light"
        assert settings.get("window_opacity") == 0.85
        assert settings.get("resource_warning_enabled") is False
        assert settings.get("model_table_column_widths") == [120, 80]
    finally:
        reopened.close()


def test_manifest_preview_does_not_write_until_applied(store, tmp_path):
    model = {
        "file_path": str(tmp_path / "imported.gguf"),
        "file_name": "imported.gguf",
        "format": "gguf",
        "size_bytes": 123,
        "provider": "local-file",
        "model_id": "imported-model",
        "display_name": "导入模型",
    }
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps({
        "schema": MODEL_SCHEMA,
        "version": TRANSFER_VERSION,
        "models": [model],
        "preferences": {"imported-model": {
            "favorite": True, "pinned": True, "tags": ["测试"], "notes": "仅数据",
        }},
    }), encoding="utf-8")
    transfer = TransferService(store)
    preview = transfer.import_models(source, apply=False)
    assert preview.data["applied"] is False
    assert store.all() == []
    applied = transfer.import_models(source, apply=True)
    assert applied.data["applied"] is True
    assert store.all()[0].source_type == "import"
    preferences = store.prefs.get("imported-model")
    assert preferences["favorite"] is True
    assert preferences["pinned"] is True
    assert preferences["tags"] == ["测试"]
    assert preferences["notes"] == "仅数据"


def test_config_preview_does_not_write_or_scan_until_applied(store, tmp_path):
    scan_root = tmp_path / "must-not-be-scanned"
    source = tmp_path / "config.json"
    source.write_text(json.dumps({
        "schema": CONFIG_SCHEMA,
        "version": TRANSFER_VERSION,
        "settings": {"theme": "light", "status_poll_interval_ms": 7000},
        "load_presets": [{
            "name": " imported ", "model_id": "model-a", "provider": "local-file",
            "device": "gpu", "context_length": 4096, "gpu_layers": 12,
            "threads": 4, "options": {"note": "命令文本也只是数据"},
        }],
        "scan_roots": [{"path": str(scan_root), "enabled": True}],
    }), encoding="utf-8")
    transfer = TransferService(store)
    preview = transfer.import_config(source, apply=False)
    assert preview.data["applied"] is False
    assert store.settings.all() == {}
    assert store.connection.execute("SELECT COUNT(*) FROM load_presets").fetchone()[0] == 0
    assert store.scan_roots.list() == []
    assert not scan_root.exists()
    applied = transfer.import_config(source, apply=True)
    assert applied.data["applied"] is True
    assert store.settings.get("theme") == "light"
    assert store.settings.get("status_poll_interval_ms") == 7000
    row = store.connection.execute("SELECT * FROM load_presets").fetchone()
    assert row["name"] == " imported "
    assert row["context_length"] == 4096
    assert json.loads(row["options"])["note"] == "命令文本也只是数据"
    assert [item["path"] for item in store.scan_roots.list()] == [str(scan_root)]
    assert not scan_root.exists()
