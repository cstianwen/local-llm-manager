"""可审查命令计划；默认只展示/复制，显式确认后才允许白名单 CLI。"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.processes import ManagedProcess, ProcessSupervisor
from .errors import CommandSecurityError


@dataclass(frozen=True)
class CommandPlan:
    title: str
    program: str
    args: tuple[str, ...] = ()
    cwd: str = ""
    impact: str = ""
    risk: str = ""
    purpose: str = ""
    kind: str = "copy"                 # copy / open_terminal / cli / companion
    origin: str = "user"               # user / imported / agent
    allow_execute: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    @property
    def command_text(self) -> str:
        parts = [self.program, *self.args]
        return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["args"] = list(self.args)
        result["command_text"] = self.command_text
        return result


@dataclass(frozen=True)
class CommandConfirmation:
    plan_id: str
    digest: str
    expires_at: float
    token: str = field(repr=False)


@dataclass(frozen=True)
class CommandExecutionResult:
    plan_id: str
    success: bool
    status: str
    message: str
    process_id: str = ""
    pid: int = 0
    log_path: str = ""


class CommandCenter:
    """白名单默认为空；导入计划永远不能直接进入执行路径。"""

    def __init__(
        self,
        supervisor: ProcessSupervisor | None = None,
        *,
        allowed_executables: Iterable[str | os.PathLike[str]] = (),
        confirmation_ttl_seconds: float = 300.0,
    ):
        self.supervisor = supervisor
        self.confirmation_ttl_seconds = max(1.0, float(confirmation_ttl_seconds))
        self._plans: dict[str, CommandPlan] = {}
        self._confirmations: dict[str, CommandConfirmation] = {}
        self._allowed: set[str] = set()
        self._lock = threading.RLock()
        for executable in allowed_executables:
            self.register_allowed_executable(executable)

    @staticmethod
    def _key(path: str | os.PathLike[str]) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))

    @property
    def allowed_executables(self) -> tuple[str, ...]:
        return tuple(sorted(self._allowed))

    def register_allowed_executable(self, executable: str | os.PathLike[str]) -> str:
        candidate = Path(executable).expanduser()
        if not candidate.is_absolute():
            raise CommandSecurityError("白名单程序必须使用绝对路径，避免 PATH 劫持")
        if candidate.suffix.casefold() in {".bat", ".cmd", ".ps1", ".com", ".sh"}:
            raise CommandSecurityError("白名单不接受脚本或 shell 批处理程序")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise CommandSecurityError(f"白名单程序不存在或无法解析：{candidate}") from exc
        if not resolved.is_file():
            raise CommandSecurityError("白名单程序必须是普通文件")
        key = self._key(resolved)
        self._allowed.add(key)
        return str(resolved)

    def revoke_allowed_executable(self, executable: str | os.PathLike[str]) -> None:
        self._allowed.discard(self._key(executable))

    def create_plan(
        self,
        title: str,
        program: str,
        args: Iterable[str] = (),
        *,
        cwd: str | os.PathLike[str] = "",
        impact: str = "",
        risk: str = "",
        purpose: str = "",
        kind: str = "copy",
        origin: str = "user",
        allow_execute: bool = False,
    ) -> CommandPlan:
        if isinstance(args, (str, bytes)):
            raise ValueError("args 必须是逐参数序列，不能接受命令字符串")
        arguments = tuple(args)
        if not program.strip() or any(not isinstance(item, str) or not item for item in arguments):
            raise ValueError("程序及每个参数必须是非空字符串")
        if origin == "imported" and allow_execute:
            raise CommandSecurityError("导入场景中的命令只作为数据，不能标记为可执行")
        plan = CommandPlan(
            title=title.strip() or "命令计划", program=program.strip(), args=arguments,
            cwd=os.fspath(cwd), impact=impact, risk=risk, purpose=purpose,
            kind=kind, origin=origin, allow_execute=bool(allow_execute),
        )
        with self._lock:
            self._plans[plan.id] = plan
        return plan

    def create_imported_plan(self, data: dict[str, Any]) -> CommandPlan:
        """用于展示导入数据；即使调用方误传 allow_execute 也不会执行。"""
        return self.create_plan(
            title=str(data.get("title", "导入命令（仅数据）")),
            program=str(data.get("program", "")),
            args=tuple(str(item) for item in data.get("args", [])),
            cwd=str(data.get("cwd", "")), impact=str(data.get("impact", "未执行，仅展示导入数据")),
            risk=str(data.get("risk", "导入内容不可信")), purpose=str(data.get("purpose", "")),
            kind="imported", origin="imported", allow_execute=False,
        )

    def create_terminal_plan(self, cwd: str | os.PathLike[str], terminal: str = "wt.exe") -> CommandPlan:
        workdir = os.fspath(cwd)
        return self.create_plan(
            title="打开终端（默认仅展示/复制）", program=terminal, args=("-d", workdir),
            cwd=workdir, impact="在用户明确操作时打开终端并切换目录；当前不会自动启动。",
            risk="终端中的后续命令由用户自行审查。", kind="open_terminal",
        )

    def create_companion_plan(
        self, executable: str, *, cwd: str = "", purpose: str = ""
    ) -> CommandPlan:
        return self.create_plan(
            title="启动配套软件（需另行显式确认与白名单）", program=executable, cwd=cwd,
            impact="启动一个由 ProcessSupervisor 监管的直接子进程。",
            risk="第三方软件可能读写其自身项目或生成文件；本工具不声称处理已完成。",
            purpose=purpose, kind="companion", allow_execute=True,
        )

    def get_plan(self, plan_id: str) -> CommandPlan:
        try:
            return self._plans[plan_id]
        except KeyError as exc:
            raise KeyError(f"未知命令计划：{plan_id}") from exc

    def copy_text(self, plan_id: str) -> str:
        return self.get_plan(plan_id).command_text

    @staticmethod
    def _digest(plan: CommandPlan) -> str:
        payload = {
            "id": plan.id, "program": plan.program, "args": plan.args, "cwd": plan.cwd,
            "kind": plan.kind, "origin": plan.origin, "allow_execute": plan.allow_execute,
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def request_confirmation(self, plan_id: str) -> CommandConfirmation:
        plan = self.get_plan(plan_id)
        confirmation = CommandConfirmation(
            plan_id=plan.id, digest=self._digest(plan),
            expires_at=time.time() + self.confirmation_ttl_seconds,
            token=secrets.token_urlsafe(32),
        )
        with self._lock:
            self._confirmations[plan.id] = confirmation
        return confirmation

    def execute_plan(
        self,
        plan_id: str,
        confirmation: CommandConfirmation,
        *,
        on_log: Callable[[ManagedProcess, str], None] | None = None,
    ) -> CommandExecutionResult:
        plan = self.get_plan(plan_id)
        with self._lock:
            expected = self._confirmations.get(plan_id)
            valid = (
                isinstance(confirmation, CommandConfirmation)
                and expected is not None
                and confirmation.plan_id == plan_id
                and confirmation.digest == expected.digest == self._digest(plan)
                and secrets.compare_digest(confirmation.token, expected.token)
                and confirmation.expires_at == expected.expires_at
            )
            if valid:
                self._confirmations.pop(plan_id, None)
        if not valid:
            return CommandExecutionResult(plan_id, False, "blocked", "命令确认令牌无效或已使用；未启动")
        if time.time() > expected.expires_at:
            return CommandExecutionResult(plan_id, False, "blocked", "命令确认已过期；未启动")
        try:
            if not plan.allow_execute or plan.origin == "imported" or plan.kind not in {"cli", "companion"}:
                raise CommandSecurityError("该计划只允许展示、复制或打开终端指引，不能由核心执行")
            executable = Path(plan.program).expanduser()
            if not executable.is_absolute():
                raise CommandSecurityError("执行程序必须使用绝对路径")
            resolved = executable.resolve(strict=True)
            if self._key(resolved) not in self._allowed:
                raise CommandSecurityError(f"程序不在显式白名单中：{resolved}")
            if self.supervisor is None:
                raise CommandSecurityError("未配置 ProcessSupervisor，不能启动命令")
            process = self.supervisor.start(
                [str(resolved), *plan.args], cwd=plan.cwd or None,
                provider="agent", purpose=plan.purpose or plan.title, on_log=on_log,
            )
        except (OSError, ValueError, CommandSecurityError) as exc:
            return CommandExecutionResult(plan_id, False, "blocked", f"命令未启动：{exc}")
        return CommandExecutionResult(
            plan_id, True, "started",
            f"已启动受管子进程 PID={process.pid}；started 不代表命令已成功完成。",
            process_id=process.id, pid=process.pid, log_path=process.log_path,
        )
