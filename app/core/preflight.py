"""模型装入前的粗略容量与后端状态预检，不承诺装入成功。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .hardware import GPUInfo, HardwareCollector, HardwareSnapshot
from .models import DictSerializable, ModelInfo, human_size


@dataclass
class PreflightResult(DictSerializable):
    provider: str
    model: str = ""
    device: str = "auto"
    selected_device: str = "unknown"
    model_size_bytes: int = 0
    available_ram_bytes: int = 0
    available_vram_bytes: int | None = None
    estimated_ram_bytes: int = 0
    estimated_vram_bytes: int = 0
    context_length: int = 0
    status: str = "unknown"  # likely / warning / insufficient / unknown
    confidence: str = "low"  # low / medium；粗略估算不提供高置信度或成功保证
    can_attempt: bool = True
    success_guaranteed: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    message: str = ""


def _model_details(model: ModelInfo | str | os.PathLike[str] | int) -> tuple[str, int]:
    if isinstance(model, ModelInfo):
        return model.model_ref or model.display_name or model.file_name, max(0, model.size_bytes)
    if isinstance(model, int):
        return "", max(0, model)
    path = Path(model).expanduser()
    if path.is_file():
        return path.name, path.stat().st_size
    if path.is_dir():
        size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        return path.name, size
    return str(model), 0


def _best_gpu(snapshot: HardwareSnapshot) -> GPUInfo | None:
    candidates = [gpu for gpu in snapshot.gpus if gpu.free_vram_bytes is not None]
    if not candidates:
        return max(snapshot.gpus, key=lambda gpu: gpu.total_vram_bytes or 0, default=None)
    return max(candidates, key=lambda gpu: gpu.free_vram_bytes or 0)


class PreflightChecker:
    """以模型文件大小为基础的保守估算；真实占用受架构、后端与 KV cache 等影响。"""

    def __init__(
        self,
        hardware: HardwareSnapshot | None = None,
        *,
        collector: HardwareCollector | None = None,
        check_backend: bool = True,
    ):
        self._hardware = hardware
        self._collector = collector
        self.check_backend = check_backend

    def check(
        self,
        model: ModelInfo | str | os.PathLike[str] | int,
        *,
        provider: str = "ollama",
        device: str = "auto",
        context_length: int = 0,
        hardware: HardwareSnapshot | None = None,
    ) -> PreflightResult:
        name, model_size = _model_details(model)
        if context_length < 0:
            raise ValueError("上下文长度不能为负数")
        requested_device = device.lower().strip() or "auto"
        if requested_device not in {"auto", "cpu", "gpu", "cuda", "nvidia"}:
            raise ValueError("device 仅支持 auto/cpu/gpu/cuda/nvidia")
        snapshot = hardware or self._hardware
        if snapshot is None:
            snapshot = (self._collector or HardwareCollector(include_ollama=self.check_backend)).collect()
        result = PreflightResult(
            provider=provider, model=name, device=requested_device,
            model_size_bytes=model_size,
            available_ram_bytes=max(0, snapshot.memory.available_bytes),
            context_length=int(context_length),
            assumptions=[
                "这是基于当前可用容量和模型文件大小的粗略估算，不是装入成功保证。",
                "模型权重估算未精确覆盖后端运行时、临时缓冲区、驱动开销与碎片。",
            ],
        )
        result.warnings.extend(snapshot.warnings)
        result.errors.extend(snapshot.errors)

        if provider.lower() == "ollama" and self.check_backend:
            if not snapshot.ollama.installed:
                result.errors.append("未检测到 Ollama 安装或本机服务，不能声明可以装入。")
                result.recommendations.append("安装并由用户自行启动 Ollama 后重试。")
                result.can_attempt = False
            elif not snapshot.ollama.running:
                result.errors.append("Ollama 已安装但本机服务未运行。")
                result.recommendations.append("由用户启动本机 Ollama 服务后重试。")
                result.can_attempt = False

        if model_size <= 0:
            result.warnings.append("模型大小未知，无法给出可靠容量估算。")
            result.recommendations.append("先读取模型文件或 Ollama show/tags 元数据，再重新预检。")
        if snapshot.memory.total_bytes <= 0:
            result.errors.append("物理内存信息不可用，无法验证系统内存容量。")
            result.can_attempt = False

        # KV cache 的架构差异很大，这里仅作为额外余量，并在结果中公开假设。
        context_bytes = int(context_length) * 256 * 1024
        runtime_bytes = max(512 * 1024 * 1024, min(model_size // 10, 2 * 1024 * 1024 * 1024))
        if context_bytes:
            result.assumptions.append("上下文额外按 256 KiB/token 粗略预留；实际 KV cache 可能显著不同。")
        full_gpu = int(model_size * 1.10) + context_bytes
        cpu_ram = int(model_size * 1.20) + runtime_bytes + context_bytes
        gpu = _best_gpu(snapshot)
        if len(snapshot.gpus) > 1:
            result.warnings.append("检测到多显卡；本预检按最大单卡估算，不假设显存可以自动合并。")

        use_gpu = requested_device in {"gpu", "cuda", "nvidia"}
        if requested_device == "auto" and gpu is not None and gpu.free_vram_bytes is not None:
            use_gpu = model_size > 0 and (gpu.free_vram_bytes or 0) >= full_gpu
            if not use_gpu:
                result.warnings.append("最大单卡当前空闲显存不足以容纳完整权重，自动模式改按 CPU 内存做保守估算；实际后端可能部分卸载。")

        if use_gpu:
            result.selected_device = gpu.name if gpu else "GPU 未知"
            if gpu is None:
                result.errors.append("请求使用 GPU，但未检测到可用显卡。")
                result.can_attempt = False
            else:
                result.available_vram_bytes = gpu.free_vram_bytes
                result.estimated_vram_bytes = full_gpu
                result.estimated_ram_bytes = runtime_bytes + context_bytes + int(model_size * 0.10)
                if gpu.free_vram_bytes is None:
                    result.warnings.append(f"{gpu.name} 的实时空闲显存未知，不能判断是否足够。")
                    result.confidence = "low"
                elif model_size > 0 and full_gpu > (gpu.free_vram_bytes or 0):
                    result.errors.append(
                        f"估算显存需求 {human_size(full_gpu)} 超过 {gpu.name} 当前空闲显存 "
                        f"{human_size(gpu.free_vram_bytes or 0)}。"
                    )
                    result.can_attempt = False
                    result.recommendations.append("降低上下文/量化规模，释放显存，或改用 CPU/部分卸载后重新预检。")
                if gpu.confidence != "exact":
                    result.warnings.append("显存容量来自 WMI/CIM 等近似来源，预检置信度较低。")
        else:
            result.selected_device = "CPU/系统内存"
            result.estimated_ram_bytes = cpu_ram
            if requested_device == "auto" and gpu is not None:
                result.available_vram_bytes = gpu.free_vram_bytes
                result.estimated_vram_bytes = full_gpu

        if model_size > 0 and result.estimated_ram_bytes > result.available_ram_bytes:
            result.errors.append(
                f"估算内存需求 {human_size(result.estimated_ram_bytes)} 超过当前可用内存 "
                f"{human_size(result.available_ram_bytes)}。"
            )
            result.can_attempt = False
            result.recommendations.append("关闭占用内存的应用、选择更小的量化版本或降低上下文。")
        elif model_size > 0 and result.available_ram_bytes > 0:
            remaining = result.available_ram_bytes - result.estimated_ram_bytes
            if remaining < max(1024 * 1024 * 1024, result.estimated_ram_bytes // 10):
                result.warnings.append("估算后剩余内存较少，系统换页或其他应用可能导致装入失败/卡顿。")

        if result.errors:
            result.status = "insufficient" if result.can_attempt is False and model_size > 0 else "unknown"
            result.message = "预检发现明确阻塞或容量不足；未执行装入。"
        elif model_size <= 0 or snapshot.memory.total_bytes <= 0:
            result.status = "unknown"
            result.message = "信息不足，只能由用户决定是否尝试；不能保证成功。"
        elif result.warnings:
            result.status = "warning"
            result.message = "容量粗略估算可尝试，但存在警告；实际装入仍可能失败。"
        else:
            result.status = "likely"
            result.message = "按粗略估算当前容量较充足，但这不代表装入一定成功。"
        exact_gpu = not use_gpu or (gpu is not None and gpu.confidence == "exact" and gpu.free_vram_bytes is not None)
        result.confidence = "medium" if model_size > 0 and snapshot.memory.total_bytes > 0 and exact_gpu else "low"
        return result

    preflight = check


def preflight_model(
    model: ModelInfo | str | os.PathLike[str] | int,
    *,
    provider: str = "ollama",
    device: str = "auto",
    context_length: int = 0,
    hardware: HardwareSnapshot | None = None,
    check_backend: bool = True,
) -> PreflightResult:
    return PreflightChecker(hardware, check_backend=check_backend).check(
        model, provider=provider, device=device, context_length=context_length,
    )


preflight_load = preflight_model
