import threading
import time

from app.core.settings import Settings
from app.core.tasks import TaskManager
from app.ui.dispatch import GuiDispatch
from app.ui.scan_scope_dialog import ScanScopeDialog


def _pump(qapp, predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_task_keeps_object_alive_and_clears_on_main_thread(qapp):
    manager = TaskManager(max_threads=1)
    seen = []

    def worker(token, progress):
        progress({"message": "进行中"})
        return 42

    task = manager.submit("lifecycle", worker)
    assert task is not None
    assert task.autoDelete() is False
    task.signals.result.connect(lambda value: seen.append(value))
    assert manager.wait_for_done(5000)
    assert _pump(qapp, lambda: not manager.active_keys)
    assert manager.active_keys == ()
    assert 42 in seen
    # 已完成任务转入 retired，由 Python 持有，不被 Qt 提前释放。
    assert "lifecycle" in [item.key for item in manager._retired]


def test_gui_dispatch_runs_callback_on_main_thread(qapp):
    dispatch = GuiDispatch()
    results = []

    def post_from_thread():
        dispatch.post(lambda: results.append(threading.current_thread() is threading.main_thread()))

    thread = threading.Thread(target=post_from_thread)
    thread.start()
    thread.join()
    assert _pump(qapp, lambda: bool(results))
    assert results == [True]


def test_scan_scope_dialog_only_returns_checked_paths(qapp, store, tmp_path):
    settings = Settings(store)
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    dialog = ScanScopeDialog(store, [], settings)
    dialog._row("已添加目录", model_dir)
    assert not dialog.start_button.isEnabled()
    dialog._set_checked(str(model_dir), True)
    assert dialog.checked_roots() == [str(model_dir)]
    assert dialog.start_button.isEnabled()
    dialog.accept()
    assert settings.get("scan_scope_selection") == [str(model_dir)]


def test_settings_default_theme_is_dark_and_legacy_accent_is_fixed(store):
    settings = Settings(store)
    assert settings.get("theme") == "dark"
    settings.set("accent_color", "#159da8")
    assert Settings(store).get("accent_color") == "#4f8cff"
