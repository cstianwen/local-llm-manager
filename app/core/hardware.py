"""无 UI 依赖的硬件快照：CPU、内存、GPU 与本机 Ollama 状态。"""
from __future__ import annotations

import csv
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import DictSerializable

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore


@dataclass
class CPUInfo(DictSerializable):
    name: str = ""
    architecture: str = ""
    logical_cores: int = 0
    physical_cores: int | None = None
    max_frequency_mhz: float = 0.0
    source: str = "unknown"


@dataclass
class MemoryInfo(DictSerializable):
    total_bytes: int = 0
    available_bytes: int = 0
    used_bytes: int = 0
    percent: float = 0.0
    source: str = "unknown"


@dataclass
class GPUInfo(DictSerializable):
    name: str
    vendor: str = ""
    total_vram_bytes: int | None = None
    free_vram_bytes: int | None = None
    driver_version: str = ""
    source: str = "unknown"
    confidence: str = "unknown"  # exact / inferred / unknown
    warning: str = ""


@dataclass
class OllamaStatus(DictSerializable):
    installed: bool = False
    running: bool = False
    version: str = ""
    executable: str = ""
    base_url: str = "http://127.0.0.1:11434"
    message: str = ""
    error: str = ""


@dataclass
class HardwareSnapshot(DictSerializable):
    captured_at: float = field(default_factory=time.time)
    cpu: CPUInfo = field(default_factory=CPUInfo)
    memory: MemoryInfo = field(default_factory=MemoryInfo)
    gpus: list[GPUInfo] = field(default_factory=list)
    ollama: OllamaStatus = field(default_factory=OllamaStatus)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dependencies: dict[str, bool] = field(default_factory=dict)

    @property
    def total_vram_bytes(self) -> int:
        return sum(gpu.total_vram_bytes or 0 for gpu in self.gpus)

    @property
    def available_vram_bytes(self) -> int:
        # 多显卡显存不能默认拼接；该聚合仅供展示，预检使用最大单卡可用值。
        return sum(gpu.free_vram_bytes or 0 for gpu in self.gpus)


def _hidden_process_kwargs() -> dict[str, Any]:
    if sys.platform.startswith("win"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        timeout=timeout, **_hidden_process_kwargs(),
    )


def _memory_windows() -> MemoryInfo:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong), ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.length = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx 调用失败")
    total = int(status.total_physical)
    available = int(status.available_physical)
    return MemoryInfo(
        total_bytes=total, available_bytes=available,
        used_bytes=max(0, total - available), percent=float(status.memory_load),
        source="Windows GlobalMemoryStatusEx",
    )


def _memory_proc() -> MemoryInfo:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, raw = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable"}:
            values[key] = int(raw.strip().split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    if not total:
        raise OSError("/proc/meminfo 缺少 MemTotal")
    return MemoryInfo(
        total_bytes=total, available_bytes=available, used_bytes=max(0, total - available),
        percent=round((total - available) * 100.0 / total, 1) if total else 0.0,
        source="/proc/meminfo",
    )


def collect_memory() -> MemoryInfo:
    if psutil is not None:
        memory = psutil.virtual_memory()
        return MemoryInfo(
            total_bytes=int(memory.total), available_bytes=int(memory.available),
            used_bytes=int(memory.used), percent=float(memory.percent), source="psutil",
        )
    if sys.platform.startswith("win"):
        return _memory_windows()
    return _memory_proc()


def collect_cpu() -> CPUInfo:
    name = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "") or platform.machine()
    info = CPUInfo(
        name=name, architecture=platform.machine(), logical_cores=os.cpu_count() or 0,
        source="Python platform/os",
    )
    if psutil is not None:
        try:
            info.physical_cores = psutil.cpu_count(logical=False)
            info.logical_cores = psutil.cpu_count(logical=True) or info.logical_cores
            frequency = psutil.cpu_freq()
            if frequency:
                info.max_frequency_mhz = float(frequency.max or frequency.current or 0.0)
            info.source = "psutil + Python platform"
        except (psutil.Error, OSError):
            pass
    return info


def _vendor(name: str) -> str:
    lowered = name.lower()
    if "nvidia" in lowered or "geforce" in lowered or "quadro" in lowered:
        return "NVIDIA"
    if "amd" in lowered or "radeon" in lowered:
        return "AMD"
    if "intel" in lowered or "arc" in lowered or "uhd" in lowered or "iris" in lowered:
        return "Intel"
    return ""


def collect_nvidia_gpus(timeout: float = 5.0) -> tuple[list[GPUInfo], list[str]]:
    executable = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if not executable:
        return [], []
    command = [
        executable,
        "--query-gpu=name,driver_version,memory.total,memory.free",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = _run(command, timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], [f"nvidia-smi 调用失败：{exc}"]
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"返回码 {completed.returncode}"
        return [], [f"nvidia-smi 未返回显卡数据：{detail}"]
    gpus = []
    warnings = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            name, driver, total, free = next(csv.reader([line], skipinitialspace=True))
            gpus.append(GPUInfo(
                name=name.strip(), vendor="NVIDIA", driver_version=driver.strip(),
                total_vram_bytes=int(float(total.strip()) * 1024 * 1024),
                free_vram_bytes=int(float(free.strip()) * 1024 * 1024),
                source="nvidia-smi", confidence="exact",
            ))
        except (ValueError, csv.Error) as exc:
            warnings.append(f"无法解析 nvidia-smi 行 {line!r}：{exc}")
    return gpus, warnings


def collect_windows_gpus(timeout: float = 8.0) -> tuple[list[GPUInfo], list[str]]:
    """WMI/CIM 只能提供近似适配器容量，不能提供实时空闲显存。"""
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if not executable:
        return [], ["未找到 PowerShell，无法执行 Windows WMI/CIM 显卡回退查询。"]
    script = (
        "$items = @(Get-CimInstance Win32_VideoController | "
        "Select-Object Name, AdapterRAM, DriverVersion, VideoProcessor); "
        "ConvertTo-Json -InputObject $items -Compress"
    )
    try:
        completed = _run([executable, "-NoProfile", "-NonInteractive", "-Command", script], timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], [f"PowerShell 显卡查询失败：{exc}"]
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"返回码 {completed.returncode}"
        return [], [f"Windows WMI/CIM 显卡查询失败：{detail}"]
    try:
        payload = json.loads(completed.stdout or "[]")
    except ValueError as exc:
        return [], [f"Windows 显卡查询返回无效 JSON：{exc}"]
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return [], ["Windows 显卡查询返回了无法识别的结构。"]
    gpus = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or item.get("VideoProcessor") or "未知显卡")
        adapter_ram = item.get("AdapterRAM")
        total = None
        warning = "WMI/CIM 不提供实时空闲显存。"
        try:
            if adapter_ram is not None:
                total = max(0, int(adapter_ram))
                if total >= 0xFFFFFF00:
                    total = None
                    warning += " AdapterRAM 超过 32 位字段范围，容量标记为未知。"
                else:
                    warning += " AdapterRAM 为近似容量，大显存显卡可能不准确。"
        except (TypeError, ValueError):
            warning += " AdapterRAM 无法解析。"
        gpus.append(GPUInfo(
            name=name, vendor=_vendor(name), total_vram_bytes=total,
            free_vram_bytes=None, driver_version=str(item.get("DriverVersion") or ""),
            source="Windows WMI/CIM Win32_VideoController", confidence="inferred", warning=warning,
        ))
    return gpus, []


class HardwareCollector:
    def __init__(self, *, include_ollama: bool = True, ollama_url: str = "http://127.0.0.1:11434"):
        self.include_ollama = include_ollama
        self.ollama_url = ollama_url

    def collect(self) -> HardwareSnapshot:
        snapshot = HardwareSnapshot(dependencies={
            "psutil": psutil is not None,
            "nvidia-smi": bool(shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")),
            "powershell": bool(shutil.which("powershell") or shutil.which("pwsh")),
        })
        if psutil is None:
            snapshot.warnings.append("未安装 psutil：CPU 物理核心/频率可能未知，内存使用系统原生接口降级采集。")
        try:
            snapshot.cpu = collect_cpu()
        except Exception as exc:
            snapshot.errors.append(f"CPU 采集失败：{exc}")
        try:
            snapshot.memory = collect_memory()
        except Exception as exc:
            snapshot.errors.append(f"内存采集失败：{exc}")
        try:
            gpus, warnings = collect_nvidia_gpus()
            snapshot.warnings.extend(warnings)
            if not gpus and sys.platform.startswith("win"):
                gpus, warnings = collect_windows_gpus()
                snapshot.warnings.extend(warnings)
            snapshot.gpus = gpus
            for gpu in gpus:
                if gpu.warning:
                    snapshot.warnings.append(f"{gpu.name}：{gpu.warning}")
            if not gpus:
                snapshot.warnings.append("未获得可用显卡信息；不代表没有显卡，显存预检置信度将降低。")
        except Exception as exc:
            snapshot.errors.append(f"显卡采集失败：{exc}")
        if self.include_ollama:
            try:
                from ..providers.ollama import OllamaProvider
                status = OllamaProvider(self.ollama_url, timeout=2.0).detect()
                snapshot.ollama = OllamaStatus(
                    installed=status.installed, running=status.running,
                    version=status.version, executable=status.executable,
                    base_url=status.base_url, message=status.message, error=status.error,
                )
            except Exception as exc:
                snapshot.ollama.error = str(exc)
                snapshot.errors.append(f"Ollama 状态采集失败：{exc}")
        return snapshot

    snapshot = collect


def collect_hardware(*, include_ollama: bool = True) -> HardwareSnapshot:
    return HardwareCollector(include_ollama=include_ollama).collect()


get_hardware_snapshot = collect_hardware
