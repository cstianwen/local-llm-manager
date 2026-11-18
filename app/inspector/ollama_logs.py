"""读取本机 Ollama 日志并标注 PID、端口和模型关联证据。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..core.models import DictSerializable
from .base import Confidence, InspectionResult, resolve_process
from .ports import PortInspector

_PID_RE = re.compile(r"(?:\bpid\s*[=: ]\s*|\bprocess\s+id\s*[=: ]\s*)(\d{1,10})", re.IGNORECASE)
_PORT_RE = re.compile(r"(?:\bport\s*[=: ]\s*|(?:127\.0\.0\.1|localhost|\[::1\]):)(\d{1,5})", re.IGNORECASE)


@dataclass
class OllamaLogEntry(DictSerializable):
    log_path: str
    line_number: int
    line: str
    pid: int = 0
    port: int = 0
    model: str = ""
    confidence: Confidence = Confidence.EXACT


@dataclass
class OllamaInspectionResult(InspectionResult):
    entries: list[OllamaLogEntry] = field(default_factory=list)
    log_files: list[str] = field(default_factory=list)


def default_ollama_log_paths() -> list[Path]:
    paths = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        root = Path(local) / "Ollama"
        paths.extend([root / "server.log", root / "app.log"])
        if root.is_dir():
            paths.extend(sorted(root.glob("*.log")))
    paths.append(Path.home() / ".ollama" / "logs" / "server.log")
    unique = []
    seen = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


class OllamaLogInspector:
    def __init__(
        self,
        log_paths: Iterable[str | os.PathLike[str]] | None = None,
        *,
        port_inspector: PortInspector | None = None,
    ):
        self._paths = [Path(path) for path in log_paths] if log_paths is not None else None
        self._ports = port_inspector or PortInspector()

    def _log_files(self) -> list[Path]:
        candidates = self._paths if self._paths is not None else default_ollama_log_paths()
        files = []
        for path in candidates:
            if path.is_dir():
                files.extend(sorted(path.glob("*.log")))
            elif path.is_file():
                files.append(path)
        return list(dict.fromkeys(files))

    @staticmethod
    def _tail(path: Path, max_lines: int, max_bytes: int) -> list[str]:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            raw = stream.read(max_bytes)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]  # 丢弃可能不完整的 UTF-8/首行。
        return lines[-max_lines:]

    @staticmethod
    def _entry(path: Path, number: int, line: str, model: str) -> OllamaLogEntry:
        pid_match = _PID_RE.search(line)
        port_match = _PORT_RE.search(line)
        port = int(port_match.group(1)) if port_match else 0
        return OllamaLogEntry(
            log_path=str(path), line_number=number, line=line,
            pid=int(pid_match.group(1)) if pid_match else 0,
            port=port if 1 <= port <= 65535 else 0,
            model=model if model and model.casefold() in line.casefold() else "",
        )

    def inspect(
        self,
        *, model: str = "", port: int | None = None,
        max_lines: int = 1000, max_bytes: int = 4 * 1024 * 1024,
        inspect_ports: bool = True,
    ) -> OllamaInspectionResult:
        if max_lines <= 0 or max_bytes <= 0:
            raise ValueError("日志读取行数和字节限制必须大于 0")
        if port is not None and not 1 <= int(port) <= 65535:
            raise ValueError("端口必须在 1 到 65535 之间")
        target = model or (f"port:{port}" if port else "ollama-logs")
        result = OllamaInspectionResult(target=target)
        files = self._log_files()
        result.log_files = [str(path) for path in files]
        if not files:
            result.evidence.append("未找到本机 Ollama 日志文件。")
        for path in files:
            try:
                lines = self._tail(path, max_lines, max_bytes)
            except PermissionError as exc:
                result.requires_elevation = True
                result.errors.append(f"读取 Ollama 日志需要权限：{path}：{exc}")
                continue
            except OSError as exc:
                result.errors.append(f"读取 Ollama 日志失败：{path}：{exc}")
                continue
            for index, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                model_match = bool(model and model.casefold() in line.casefold())
                port_match = bool(port and re.search(rf"(?<!\d){int(port)}(?!\d)", line))
                if model and port:
                    if not (model_match or port_match):
                        continue
                elif model and not model_match:
                    continue
                elif port and not port_match:
                    continue
                elif not model and not port and not (_PID_RE.search(line) or _PORT_RE.search(line)):
                    continue
                result.entries.append(self._entry(path, index, line, model))
        if len(result.entries) > 200:
            result.entries = result.entries[-200:]

        # 日志 PID 可能过期，进程关联只标为推断；不会把同名 PID 宣称为精确占用方。
        log_pids = sorted({entry.pid for entry in result.entries if entry.pid > 0})
        for pid in log_pids:
            user, elevation, error = resolve_process(pid, provider="ollama", model=model)
            result.users.append(user)
            result.requires_elevation = result.requires_elevation or elevation
            if error:
                result.errors.append(error)
            result.evidence.append(f"日志出现 PID={pid}；与当前进程的关联为推断，需防范 PID 复用。")

        ports = {entry.port for entry in result.entries if entry.port}
        if port:
            ports.add(int(port))
        port_exact_users = False
        if inspect_ports:
            for candidate in sorted(ports):
                direction = "any" if port == candidate else "local"
                port_result = self._ports.inspect(
                    candidate, direction=direction, provider="ollama", model=model,
                )
                result.evidence.extend(port_result.evidence)
                result.errors.extend(port_result.errors)
                result.requires_elevation = result.requires_elevation or port_result.requires_elevation
                if port_result.pids:
                    port_exact_users = True
                result.users.extend(port_result.users)

        unique = {}
        for user in result.users:
            unique[user.pid] = user
        result.users = [unique[pid] for pid in sorted(unique)]
        result.pids = sorted({user.pid for user in result.users if user.pid > 0})
        if port_exact_users and not model:
            result.confidence = Confidence.EXACT
            result.message = "端口到进程的关联来自系统 socket 表；日志事实单独列出。"
        elif result.entries or result.users:
            result.confidence = Confidence.INFERRED if result.entries else Confidence.UNKNOWN
            result.message = (
                "模型与使用方的关联含日志推断，不能视为精确占用证明。"
                if model else "日志关联为推断，未获得精确端口进程证据。"
            )
        else:
            result.confidence = Confidence.UNKNOWN
            result.message = "未找到可确认的 Ollama 使用方；未伪造结果。"
        if result.requires_elevation:
            result.message += " 部分信息需要管理员权限。"
        return result


class OllamaInspector:
    def __init__(self, log_inspector: OllamaLogInspector | None = None):
        self.logs = log_inspector or OllamaLogInspector()

    def inspect(self, model: str = "", port: int | None = 11434) -> OllamaInspectionResult:
        return self.logs.inspect(model=model, port=port)


def inspect_ollama_logs(**options) -> OllamaInspectionResult:
    return OllamaLogInspector().inspect(**options)


def find_ollama_users(model: str = "", port: int | None = 11434) -> OllamaInspectionResult:
    return OllamaInspector().inspect(model=model, port=port)
