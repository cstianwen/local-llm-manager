"""Inspector 共用结果结构与进程信息解析。"""
from __future__ import annotations

import csv
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.models import AppUser, DictSerializable

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore


class Confidence(str, Enum):
    EXACT = "exact"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


@dataclass
class InspectionResult(DictSerializable):
    target: str
    confidence: Confidence = Confidence.UNKNOWN
    users: list[AppUser] = field(default_factory=list)
    pids: list[int] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    requires_elevation: bool = False
    message: str = ""


def _tasklist_name(pid: int) -> str:
    if not sys.platform.startswith("win"):
        return ""
    executable = shutil.which("tasklist")
    if not executable:
        return ""
    try:
        completed = subprocess.run(
            [executable, "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            timeout=5.0, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0:
            return ""
        row = next(csv.reader(completed.stdout.splitlines()), [])
        return row[0].strip() if row else ""
    except (OSError, subprocess.SubprocessError, csv.Error):
        return ""


def resolve_process(
    pid: int,
    *, provider: str = "", target_port: int = 0, model: str = "",
    fallback_name: str = "",
) -> tuple[AppUser, bool, str]:
    """返回进程、是否需要提权、错误；PID 来源的精确性由调用方另行标注。"""
    user = AppUser(
        pid=int(pid), name=fallback_name, provider=provider,
        target_port=int(target_port), model=model,
    )
    if pid <= 0:
        return user, False, "PID 无效"
    if psutil is not None:
        process = psutil.Process(pid)
        requires_elevation = False
        errors = []
        for attribute, field_name in (("name", "name"), ("exe", "exe_path"), ("cmdline", "cmdline")):
            try:
                value = getattr(process, attribute)()
                if attribute == "cmdline":
                    value = " ".join(value) if isinstance(value, list) else str(value or "")
                setattr(user, field_name, str(value or ""))
            except psutil.NoSuchProcess:
                return user, False, f"PID {pid} 已退出"
            except psutil.AccessDenied:
                requires_elevation = True
                errors.append(f"读取 PID {pid} 的 {attribute} 需要管理员权限")
            except (psutil.Error, OSError) as exc:
                errors.append(f"读取 PID {pid} 的 {attribute} 失败：{exc}")
        if not user.name:
            user.name = fallback_name
        return user, requires_elevation, "；".join(errors)
    user.name = _tasklist_name(pid) or fallback_name
    if not user.name:
        return user, False, "未安装 psutil，且 tasklist 无法确认进程名称；exe/cmdline 未知"
    return user, False, "未安装 psutil，exe/cmdline 信息已降级为空"
