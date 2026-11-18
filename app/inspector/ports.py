"""通过 OS socket 表执行端口 → PID → 进程反查。"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from .base import Confidence, InspectionResult, psutil, resolve_process


@dataclass
class _SocketRow:
    protocol: str
    local_address: str
    remote_address: str
    local_port: int
    remote_port: int
    state: str
    pid: int


def _address_port(address: str) -> int:
    try:
        return int(address.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return 0


def _psutil_rows(port: int, host: str | None) -> list[_SocketRow]:
    rows = []
    for connection in psutil.net_connections(kind="inet"):
        local = connection.laddr
        remote = connection.raddr
        local_ip = str(getattr(local, "ip", "") or "")
        remote_ip = str(getattr(remote, "ip", "") or "")
        local_port = int(getattr(local, "port", 0) or 0)
        remote_port = int(getattr(remote, "port", 0) or 0)
        if port not in {local_port, remote_port}:
            continue
        if host and host not in {local_ip, remote_ip}:
            continue
        rows.append(_SocketRow(
            protocol="tcp" if connection.type == 1 else "udp",
            local_address=local_ip, remote_address=remote_ip,
            local_port=local_port, remote_port=remote_port,
            state=str(connection.status or ""), pid=int(connection.pid or 0),
        ))
    return rows


def _netstat_rows(port: int, host: str | None) -> list[_SocketRow]:
    if not sys.platform.startswith("win"):
        raise RuntimeError("未安装 psutil；非 Windows 平台没有已实现的 socket 表回退")
    executable = shutil.which("netstat")
    if not executable:
        raise RuntimeError("未安装 psutil，且未找到 Windows netstat")
    completed = subprocess.run(
        [executable, "-ano"], shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", timeout=10.0,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"netstat 返回码 {completed.returncode}")
    rows = []
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() not in {"TCP", "UDP", "TCPV6", "UDPV6"}:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        local_port = _address_port(parts[1])
        remote_port = _address_port(parts[2])
        if port not in {local_port, remote_port}:
            continue
        if host and not (parts[1].startswith(host) or parts[2].startswith(host)):
            continue
        state = parts[3] if len(parts) >= 5 else ""
        rows.append(_SocketRow(
            protocol=parts[0].lower(), local_address=parts[1], remote_address=parts[2],
            local_port=local_port, remote_port=remote_port, state=state, pid=pid,
        ))
    return rows


class PortInspector:
    def inspect(
        self,
        port: int,
        *, host: str | None = None,
        direction: str = "local",  # local / remote / any
        provider: str = "", model: str = "",
    ) -> InspectionResult:
        if not 1 <= int(port) <= 65535:
            raise ValueError("端口必须在 1 到 65535 之间")
        if direction not in {"local", "remote", "any"}:
            raise ValueError("direction 仅支持 local/remote/any")
        result = InspectionResult(target=f"{host or '*'}:{port}")
        try:
            rows = _psutil_rows(port, host) if psutil is not None else _netstat_rows(port, host)
            result.evidence.append("socket 表来源：psutil" if psutil is not None else "socket 表来源：Windows netstat -ano")
        except Exception as exc:
            access_denied = psutil is not None and isinstance(exc, psutil.AccessDenied)
            result.requires_elevation = bool(access_denied)
            result.errors.append(f"读取系统 socket 表失败：{exc}")
            if access_denied:
                try:
                    rows = _netstat_rows(port, host)
                    result.evidence.append("psutil 权限不足，已使用 Windows netstat -ano 回退")
                    result.errors.clear()
                except Exception as fallback:
                    result.errors.append(f"netstat 回退失败：{fallback}")
                    result.message = "无法确认端口使用方；请使用管理员权限重试。"
                    return result
            else:
                result.message = "无法确认端口使用方，未伪造进程关联。"
                return result

        selected = []
        for row in rows:
            matches = (
                direction == "any" or
                (direction == "local" and row.local_port == port) or
                (direction == "remote" and row.remote_port == port)
            )
            if matches:
                selected.append(row)
        result.pids = sorted({row.pid for row in selected if row.pid > 0})
        for row in selected:
            result.evidence.append(
                f"{row.protocol} {row.local_address}:{row.local_port} -> "
                f"{row.remote_address}:{row.remote_port} {row.state} PID={row.pid or '未知'}"
            )
        for pid in result.pids:
            user, elevation, error = resolve_process(
                pid, provider=provider, target_port=port, model=model,
            )
            result.users.append(user)
            result.requires_elevation = result.requires_elevation or elevation
            if error:
                result.errors.append(error)
        result.confidence = Confidence.EXACT
        if not selected:
            result.message = "本次系统 socket 快照中未发现匹配连接；这只代表当前时刻。"
        elif not result.pids:
            result.confidence = Confidence.UNKNOWN
            result.message = "发现匹配 socket，但系统未提供 PID，使用方未知。"
        else:
            result.message = "端口到 PID 的关联来自系统 socket 表，为精确关联。"
        if result.requires_elevation:
            result.message += " 部分进程详情需要管理员权限。"
        return result

    def port_to_pid(self, port: int, **options: Any) -> list[int]:
        return self.inspect(port, **options).pids

    def find_users(self, port: int, **options: Any) -> InspectionResult:
        return self.inspect(port, **options)


def find_port_users(port: int, **options: Any) -> InspectionResult:
    return PortInspector().inspect(port, **options)


def port_to_pids(port: int, **options: Any) -> list[int]:
    return find_port_users(port, **options).pids
