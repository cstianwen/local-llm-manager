import pytest
from PySide6.QtWidgets import QHeaderView, QMessageBox

from app.core.models import ModelInfo, OperationResult
from app.ui.main_window import MainWindow
from app.ui.model_page import ModelPage


@pytest.fixture
def main_window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(ModelPage, "refresh_status", lambda self: None)
    window = MainWindow()
    window.show()
    qapp.processEvents()
    try:
        yield window
    finally:
        window.close()
        window.finish_shutdown()
        qapp.processEvents()


def add_models(window):
    window.store.upsert_many([
        ModelInfo(
            file_path=r"C:\fake\a.gguf", file_name="a.gguf", format="gguf",
            size_bytes=1000, provider="local-file", model_id="model-a",
            display_name="模型 A", quant="Q4_K_M",
        ),
        ModelInfo(
            file_path=r"C:\fake\b.gguf", file_name="b.gguf", format="gguf",
            size_bytes=2000, provider="local-file", model_id="model-b",
            display_name="模型 B", quant="Q8_0",
        ),
    ])
    window.settings_page.reload_models()
    window.model_page.reload_models()


def test_four_pages_settings_models_and_window_preferences(main_window, qapp):
    window = main_window
    assert set(window.pages) == {"models", "agent", "downloads", "settings"}
    assert window.stack.count() == 4
    for name, page in window.pages.items():
        window.navigate_to(name)
        assert window.stack.currentWidget() is page
    page = window.settings_page
    assert page.tabs.count() == 4
    add_models(window)

    page.poll_seconds.setValue(7)
    page.theme_combo.setCurrentIndex(page.theme_combo.findData("light"))
    page.opacity_slider.setValue(85)
    page.density_combo.setCurrentIndex(page.density_combo.findData("compact"))
    qapp.processEvents()
    assert window.settings.get("status_poll_interval_ms") == 7000
    assert window.model_page._poll_interval_ms == 7000
    assert window.settings.get("theme") == "light"
    assert window.settings.get("window_opacity") == 0.85
    assert abs(window.windowOpacity() - 0.85) < 0.01
    assert window.model_page.table.verticalHeader().defaultSectionSize() == 24

    page.pref_model_combo.setCurrentIndex(page.pref_model_combo.findData("model-a"))
    page.pref_favorite_check.setChecked(True)
    page.pref_pinned_check.setChecked(True)
    page.pref_tags_edit.setText("常用， 编程")
    page.pref_notes_edit.setPlainText("测试备注")
    page.save_preference()
    preferences = window.store.prefs.get("model-a")
    assert preferences["favorite"] and preferences["pinned"]
    assert preferences["tags"] == ["常用", "编程"]
    assert preferences["notes"] == "测试备注"

    page.preset_name_edit.setText("smoke-preset")
    page.preset_model_combo.setCurrentIndex(page.preset_model_combo.findData("model-a"))
    page.preset_context_spin.setValue(4096)
    page.preset_gpu_layers_spin.setValue(12)
    page.preset_threads_spin.setValue(4)
    page.save_preset()
    assert page.preset_combo.currentData()["name"] == "smoke-preset"
    preset = window.store.connection.execute(
        "SELECT * FROM load_presets WHERE name='smoke-preset'"
    ).fetchone()
    assert preset["context_length"] == 4096
    assert preset["gpu_layers"] == 12
    assert preset["threads"] == 4

    window.store.usage.record(
        "model-a", "local-file", duration_ms=1250, input_tokens=11, output_tokens=22,
    )
    window.store.usage.record(
        "model-a", "local-file", duration_ms=750, input_tokens=3, output_tokens=4,
    )
    page.refresh_statistics()
    assert page.stats_table.rowCount() == 1
    assert "调用 2 次" in page.stats_summary_label.text()
    page.compare_a_combo.setCurrentIndex(page.compare_a_combo.findData("model-a"))
    page.compare_b_combo.setCurrentIndex(page.compare_b_combo.findData("model-b"))
    page.compare_models()
    assert page.compare_table.rowCount() == 10
    page.refresh_logs()
    assert page.log_table.rowCount() >= 2

    window.toggle_sidebar()
    header = window.model_page.table.horizontalHeader()
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
    window.model_page.table.setColumnWidth(1, 155)
    window.model_page.splitter.setSizes([500, 300])
    window._save_window_preferences()
    assert window.settings.get("sidebar_collapsed") is True
    assert window.settings.get("model_table_column_widths")[1] == 155
    assert window.settings.get("window_geometry")


def test_import_preview_requires_explicit_confirmation(main_window, monkeypatch):
    calls = []

    def fake_import(path, *, apply=False):
        calls.append(apply)
        return OperationResult(True, "ok", data={"model_count": 0, "applied": apply})

    monkeypatch.setattr(main_window.service, "import_models", fake_import)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Cancel,
    )
    main_window.settings_page._import_preview("unused.json", config=False)
    assert calls == [False]

    calls.clear()
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    main_window.settings_page._import_preview("unused.json", config=False)
    assert calls == [False, True]
