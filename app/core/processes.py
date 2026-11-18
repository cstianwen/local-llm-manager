"""仅回收本工具创建子进程的进程监管器，不使用 shell。"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import app_data_dir
from .errors import ModelManagerError
from .models import DictSerializable

_LOG = logging.getLogger(__name__)

try:  # psutil 用于加强 PID 复用检测，缺失时仍可靠使用直接子进程句柄。
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - 取决于运行环境
    psutil = None  # type: ignore


class ProcessNotManagedError(ModelManagerError):
    """目标不属于当前监管器，拒绝发送任何终止信号。"""


@dataclass
class ProcessSnapshot(DictSerializable):
    id: str
    pid: int
    provider: str
    model_id: str
    purpose: str
    command: list[str]
    executable: str
    cwd: str
    log_path: str
    status: str
    started_at: float
    process_created_at: float
    stopped_at: float = 0.0
    exit_code: int | None = None


@dataclass
class ManagedProcess:
    """监管器返回的活记录；快照持久化请使用 snapshot()/to_dict()。"""

    id: str
    pid: int
    command: tuple[str, ...]
    executable: str
    cwd: str
    log_path: str
    provider: str
    model_id: str
    purpose: str
    started_at: float
    process_created_at: float
    status: str = "running"
    stopped_at: float = 0.0
    exit_code: int | None = None
    _popen: subprocess.Popen[str] = field(repr=False, compare=False, default=None)  # type: ignore
    _supervisor_id: str = field(repr=False, compare=False, default="")
    _stop_requested: bool = field(repr=False, compare=False, default=False)
    _lines: deque[str] = field(repr=False, compare=False, default_factory=lambda: deque(maxlen=2000))
    _lock: threading.RLock = field(repr=False, compare=False, default_factory=threading.RLock)

    def snapshot(self) -> ProcessSnapshot:
        with self._lock:
            return ProcessSnapshot(
                id=self.id,
                pid=self.pid,
                provider=self.provider,
                model_id=self.model_id,
                purpose=self.purpose,
                command=list(self.command),
                executable=self.executable,
                cwd=self.cwd,
                log_path=self.log_path,
                status=self.status,
                started_at=self.started_at,
                process_created_at=self.process_created_at,
                stopped_at=self.stopped_at,
                exit_code=self.exit_code,
            )

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot().to_dict()

    @property
    def running(self) -> bool:
        return self.status == "running" and self._popen.poll() is None

    def stop(self, timeout: float = 5.0, force: bool = True) -> bool:
        supervisor = getattr(self, "_supervisor", None)
        if supervisor is None:
            raise ProcessNotManagedError("该进程记录没有所属监管器，拒绝终止")
        return supervisor.stop(self, timeout=timeout, force=force)

    def wait(self, timeout: float | None = None) -> int:
        return self._popen.wait(timeout=timeout)


LogCallback = Callable[[ManagedProcess, str], None]


class ProcessSupervisor:
    """管理由当前实例启动的直接子进程，并提供实时日志与退出回收。"""

    def __init__(
        self,
        log_dir: str | os.PathLike[str] | None = None,
        *,
        on_exit: Callable[[ManagedProcess], None] | None = None,
    ):
        self._id = uuid.uuid4().hex
        self._log_dir = Path(log_dir) if log_dir else app_data_dir() / "logs" / "processes"
        self._on_exit = on_exit
        self._records: dict[str, ManagedProcess] = {}
        self._pids: dict[int, str] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def managed_pids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._pids)

    @property
    def processes(self) -> list[ProcessSnapshot]:
        return self.list()

    def list(self, *, running_only: bool = False) -> list[ProcessSnapshot]:
        with self._lock:
            records = list(self._records.values())
        return [
            record.snapshot() for record in records
            if not running_only or record.running
        ]

    def is_managed(self, process: int | ManagedProcess) -> bool:
        pid = process if isinstance(process, int) else process.pid
        with self._lock:
            record_id = self._pids.get(pid)
            if record_id is None:
                return False
            record = self._records[record_id]
        return not isinstance(process, ManagedProcess) or (
            process._supervisor_id == self._id and record is process
        )

    def get(self, process: int | str | ManagedProcess) -> ManagedProcess:
        if isinstance(process, ManagedProcess):
            key = process.id
        elif isinstance(process, int):
            with self._lock:
                key = self._pids.get(process, "")
        else:
            key = process
        with self._lock:
            record = self._records.get(key)
        if record is None or (
            isinstance(process, ManagedProcess)
            and (record is not process or process._supervisor_id != self._id)
        ):
            raise ProcessNotManagedError(f"PID/进程 {process!r} 不是由本工具当前实例创建的")
        return record

    @staticmethod
    def _creation_time(process: subprocess.Popen[str]) -> float:
        if psutil is not None:
            try:
                return float(psutil.Process(process.pid).create_time())
            except (psutil.Error, OSError):
                pass
        # 对尚未回收的直接子进程，其 PID 不能被系统复用；0 表示没有额外时间戳证据。
        return 0.0

    def start(
        self,
        command: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: dict[str, str] | None = None,
        log_path: str | os.PathLike[str] | None = None,
        provider: str = "",
        model_id: str = "",
        purpose: str = "",
        on_log: LogCallback | None = None,
        hide_window: bool = True,
    ) -> ManagedProcess:
        if self._closed:
            raise ModelManagerError("进程监管器已关闭，不能启动新进程")
        if isinstance(command, (str, bytes)) or not command:
            raise ValueError("command 必须是非空参数序列；为避免注入不接受命令字符串")
        args = [os.fspath(part) for part in command]
        if any(not isinstance(part, str) or not part for part in args):
            raise ValueError("命令参数必须是非空字符串")
        workdir = str(Path(cwd).expanduser().resolve()) if cwd else ""
        if workdir and not Path(workdir).is_dir():
            raise FileNotFoundError(f"子进程工作目录不存在：{workdir}")

        executable = args[0]
        if not Path(executable).is_file():
            executable = shutil.which(executable) or ""
        if not executable:
            raise FileNotFoundError(f"找不到真实可执行文件：{args[0]}")
        args[0] = executable

        if log_path:
            output_path = Path(log_path).expanduser().resolve()
        else:
            output_path = self._log_dir / f"{int(time.time())}-{uuid.uuid4().hex[:10]}.log"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = output_path.open("a", encoding="utf-8", errors="replace", buffering=1)

        popen = None
        try:
            kwargs: dict[str, Any] = {
                "args": args,
                "cwd": workdir or None,
                "env": {**os.environ, **env} if env else None,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "shell": False,
            }
            if sys.platform.startswith("win"):
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
                if hide_window:
                    flags |= subprocess.CREATE_NO_WINDOW
                kwargs["creationflags"] = flags
            else:
                kwargs["start_new_session"] = True
            popen = subprocess.Popen(**kwargs)
        except Exception:
            log_file.close()
            raise

        now = time.time()
        record = ManagedProcess(
            id=uuid.uuid4().hex,
            pid=popen.pid,
            command=tuple(args),
            executable=executable,
            cwd=workdir,
            log_path=str(output_path),
            provider=provider,
            model_id=model_id,
            purpose=purpose,
            started_at=now,
            process_created_at=self._creation_time(popen),
            _popen=popen,
            _supervisor_id=self._id,
        )
        record._supervisor = self
        with self._lock:
            if self._closed:
                # close() 与 start() 竞争时也只终止刚由本实例创建的进程。
                popen.terminate()
                log_file.close()
                raise ModelManagerError("进程监管器已关闭，已回收新进程")
            self._records[record.id] = record
            self._pids[record.pid] = record.id
        thread = threading.Thread(
            target=self._read_output,
            args=(record, log_file, on_log),
            name=f"managed-process-{record.pid}",
            daemon=True,
        )
        self._threads[record.id] = thread
        thread.start()
        return record

    def _read_output(
        self,
        record: ManagedProcess,
        log_file: Any,
        on_log: LogCallback | None,
    ) -> None:
        try:
            log_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动 PID={record.pid} 命令={list(record.command)!r}\n")
            stream = record._popen.stdout
            if stream is not None:
                for line in iter(stream.readline, ""):
                    text = line.rstrip("\r\n")
                    with record._lock:
                        record._lines.append(text)
                    log_file.write(text + "\n")
                    if on_log is not None:
                        try:
                            on_log(record, text)
                        except Exception:
                            _LOG.exception("子进程日志回调失败")
            exit_code = record._popen.wait()
            with record._lock:
                record.exit_code = exit_code
                record.stopped_at = time.time()
                if record._stop_requested:
                    record.status = "stopped"
                else:
                    record.status = "exited" if exit_code == 0 else "failed"
            log_file.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 退出 PID={record.pid} 返回码={exit_code}\n"
            )
        except Exception as exc:
            _LOG.exception("读取受管子进程输出失败")
            with record._lock:
                if record.status == "running":
                    record.status = "unknown"
                record._lines.append(f"日志读取失败：{exc}")
            try:
                log_file.write(f"日志读取失败：{exc}\n")
            except Exception:
                pass
        finally:
            log_file.close()
            if self._on_exit is not None:
                try:
                    self._on_exit(record)
                except Exception:
                    _LOG.exception("子进程退出回调失败")

    def _validate_identity(self, record: ManagedProcess) -> bool:
        """返回 False 表示已退出；PID 被复用时明确拒绝。"""
        if record._popen.poll() is not None:
            return False
        if record._popen.pid != record.pid:
            raise ProcessNotManagedError(f"进程句柄 PID 与登记值 {record.pid} 不一致")
        if psutil is not None and record.process_created_at > 0:
            try:
                current = float(psutil.Process(record.pid).create_time())
            except psutil.NoSuchProcess:
                return False
            except psutil.Error:
                # 直接 Popen 子进程句柄仍是有效身份凭据，权限不足不扩大权限或杀其他进程。
                return True
            if abs(current - record.process_created_at) > 0.01:
                raise ProcessNotManagedError(
                    f"PID {record.pid} 的创建时间已变化，可能已被系统复用；拒绝终止"
                )
        return True

    def stop(
        self,
        process: int | str | ManagedProcess,
        *,
        timeout: float = 5.0,
        force: bool = True,
    ) -> bool:
        record = self.get(process)
        with record._lock:
            if not self._validate_identity(record):
                if record.status == "running":
                    record.status = "exited"
                return False
            record._stop_requested = True
        try:
            record._popen.terminate()
            record._popen.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            if not force:
                raise ModelManagerError(
                    f"PID {record.pid} 在 {timeout:g} 秒内未退出；未强制终止"
                )
            # 超时后再次校验，避免等待期间退出且 PID 被复用。
            if not self._validate_identity(record):
                return False
            record._popen.kill()
            record._popen.wait(timeout=5.0)
        with record._lock:
            record._stop_requested = True
        return True

    def cancel(self, process: int | str | ManagedProcess, *, timeout: float = 5.0) -> bool:
        return self.stop(process, timeout=timeout)

    def stop_all(self, *, timeout: float = 5.0, force: bool = True) -> dict[int, str]:
        results: dict[int, str] = {}
        for snapshot in self.list(running_only=True):
            try:
                stopped = self.stop(snapshot.id, timeout=timeout, force=force)
                results[snapshot.pid] = "stopped" if stopped else "already_exited"
            except Exception as exc:
                results[snapshot.pid] = str(exc)
        return results

    def read_logs(self, process: int | str | ManagedProcess, max_lines: int = 200) -> list[str]:
        record = self.get(process)
        with record._lock:
            if max_lines <= 0:
                return []
            return list(record._lines)[-max_lines:]

    def wait(self, process: int | str | ManagedProcess, timeout: float | None = None) -> int:
        return self.get(process)._popen.wait(timeout=timeout)

    def close(self, *, timeout: float = 5.0) -> dict[int, str]:
        with self._lock:
            if self._closed:
                return {}
            self._closed = True
        results = self.stop_all(timeout=timeout)
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=max(0.1, timeout))
        return results

    shutdown = close

    def __enter__(self) -> ProcessSupervisor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
