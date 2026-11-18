"""基于 Qt 线程池的协作式可取消后台任务。"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .errors import OperationCancelledError


class CancellationToken:
    def __init__(self):
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
            raise OperationCancelledError("任务已取消")


class TaskSignals(QObject):
    progress = Signal(object)
    result = Signal(object)
    error = Signal(object)
    cancelled = Signal()
    finished = Signal()


class Task(QRunnable):
    """worker 的前两个参数依次为 CancellationToken 和进度回调。"""

    def __init__(
        self,
        key: str,
        worker: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        on_finish: Callable[[str, Task], None],
    ):
        super().__init__()
        self.key = key
        self.token = CancellationToken()
        self.signals = TaskSignals()
        self._worker = worker
        self._args = args
        self._kwargs = kwargs
        self._on_finish = on_finish

    @Slot()
    def run(self) -> None:
        try:
            if self.token.cancelled:
                self.signals.cancelled.emit()
                return
            result = self._worker(
                self.token, self.signals.progress.emit,
                *self._args, **self._kwargs,
            )
            self.signals.result.emit(result)
        except OperationCancelledError:
            self.signals.cancelled.emit()
        except Exception as exc:
            self.signals.error.emit(exc)
        finally:
            self.signals.finished.emit()
            self._on_finish(self.key, self)


class TaskManager:
    def __init__(
        self,
        max_threads: int = 0,
        pool: QThreadPool | None = None,
    ):
        self._pool = pool or QThreadPool()
        if max_threads > 0:
            self._pool.setMaxThreadCount(max_threads)
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()

    @property
    def active_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._tasks)

    def submit(
        self,
        key: str,
        worker: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Task | None:
        """提交任务；key 已存在时返回 None，不重复执行。"""
        with self._lock:
            if key in self._tasks:
                return None
            task = Task(key, worker, args, kwargs, self._task_finished)
            self._tasks[key] = task
        self._pool.start(task)
        return task

    def cancel(self, key: str) -> bool:
        with self._lock:
            task = self._tasks.get(key)
        if task is None:
            return False
        task.token.cancel()
        return True

    def cancel_all(self) -> None:
        with self._lock:
            tasks = tuple(self._tasks.values())
        for task in tasks:
            task.token.cancel()

    def wait_for_done(self, timeout_ms: int | None = None) -> bool:
        if timeout_ms is None:
            self._pool.waitForDone()
            return True
        return self._pool.waitForDone(timeout_ms)

    def _task_finished(self, key: str, task: Task) -> None:
        with self._lock:
            if self._tasks.get(key) is task:
                del self._tasks[key]
