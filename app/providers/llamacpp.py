"""llama.cpp 本机服务、llama-server 启动与真实量化工具封装。"""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..core.errors import BackendOfflineError, BackendNotInstalledError, UnsupportedOperationError
from ..core.models import ModelInfo, RunningModel
from ..core.processes import ManagedProcess, ProcessSupervisor
from .base import (
    Capability,
    CapabilityState,
    LoopbackHTTPClient,
    Provider,
    ProviderAPIError,
    ProviderStatus,
    find_executable,
)


class LlamaCppProvider(Provider):
    name = "llamacpp"
    display_name = "llama.cpp"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        *,
        timeout: float = 5.0,
        server_path: str | os.PathLike[str] | None = None,
        quantizer_path: str | os.PathLike[str] | None = None,
        launcher: Sequence[str] | None = None,
        supervisor: ProcessSupervisor | None = None,
    ):
        self._client = LoopbackHTTPClient(self.name, base_url, timeout)
        self.base_url = self._client.base_url
        self._server_path = server_path
        self._quantizer_path = quantizer_path
        self._launcher = tuple(launcher) if launcher else ()
        self._supervisor = supervisor or ProcessSupervisor()
        self._owns_supervisor = supervisor is None
        self._last_status: ProviderStatus | None = None

    @property
    def server_executable(self) -> str:
        if self._launcher:
            return find_executable(self._launcher[0])
        return find_executable(self._server_path, "llama-server", "llama-server.exe")

    @property
    def quantizer_executable(self) -> str:
        return find_executable(
            self._quantizer_path,
            "llama-quantize", "llama-quantize.exe", "quantize", "quantize.exe",
        )

    def _probe(self) -> dict[str, Any]:
        try:
            payload = self._client.request_json("GET", "/health")
            if isinstance(payload, dict):
                return {"health": payload}
        except ProviderAPIError as health_error:
            try:
                models = self._client.request_json("GET", "/v1/models")
            except ProviderAPIError:
                raise health_error
            return {"models": models}
        return {}

    def detect(self) -> ProviderStatus:
        executable = self.server_executable
        installed = bool(executable)
        try:
            details = self._probe()
        except BackendOfflineError as exc:
            status = ProviderStatus(
                provider=self.name, installed=installed, running=False,
                state=CapabilityState.NOT_RUNNING if installed else CapabilityState.NOT_INSTALLED,
                executable=executable, base_url=self.base_url,
                message=(
                    "已找到 llama-server，但本机服务未运行。" if installed else
                    "未找到 llama-server，且默认本机端口没有可用 llama.cpp 服务。"
                ), error=str(exc),
            )
        except ProviderAPIError as exc:
            status = ProviderStatus(
                provider=self.name, installed=installed, running=False,
                state=CapabilityState.NOT_RUNNING if installed else CapabilityState.NOT_INSTALLED,
                executable=executable, base_url=self.base_url,
                message="本机地址有 HTTP 响应，但不能确认是兼容的 llama.cpp 服务。",
                error=str(exc),
            )
        else:
            installed = True
            status = ProviderStatus(
                provider=self.name, installed=True, running=True,
                state=CapabilityState.SUPPORTED, executable=executable,
                base_url=self.base_url, message="本机 llama.cpp 服务可用。", details=details,
            )
        self._last_status = status
        return status

    def capabilities(self) -> dict[str, Capability]:
        status = self._last_status or self.detect()
        state = CapabilityState.SUPPORTED if status.running else status.state
        reason = "本机 llama.cpp API 可用。" if status.running else status.message
        quantizer = self.quantizer_executable
        return {
            "detect": Capability(CapabilityState.SUPPORTED),
            "list": Capability(state, reason),
            "ps": Capability(state, "运行状态由 llama.cpp 当前服务模型推断。" if status.running else reason),
            "load": Capability(
                CapabilityState.SUPPORTED if self.server_executable else CapabilityState.NOT_INSTALLED,
                "可通过 llama-server 启动本地 GGUF。" if self.server_executable else "未检测到 llama-server。",
                "安装 llama.cpp 并确保 llama-server 在 PATH 中，或显式配置路径。",
            ),
            "unload": Capability(state, "只能停止由本工具创建的 llama-server 进程。" if status.running else reason),
            "quantize": Capability(
                CapabilityState.SUPPORTED if quantizer else CapabilityState.NOT_INSTALLED,
                f"使用真实工具：{quantizer}" if quantizer else "未检测到 llama-quantize/quantize。",
            ),
            "pull": Capability(CapabilityState.UNSUPPORTED, "llama.cpp 不提供模型仓库下载。"),
            "delete": Capability(CapabilityState.UNSUPPORTED, "llama.cpp Provider 不删除本地文件。"),
        }

    def list_models(self) -> list[ModelInfo]:
        payload = self._client.request_json("GET", "/v1/models")
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ProviderAPIError("llama.cpp /v1/models 响应缺少 data 数组", provider=self.name)
        result = []
        for item in rows:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            name = str(item["id"])
            path = str(item.get("path") or item.get("model") or "")
            result.append(ModelInfo(
                file_path=path or f"llamacpp://{name}", file_name=Path(path).name if path else name,
                format="gguf" if path.lower().endswith(".gguf") else "unknown",
                size_bytes=0, provider=self.name, model_ref=name,
                model_id=f"llamacpp:{name}", display_name=name,
                source_type="backend", integrity="unknown",
            ))
        return result

    def running_models(self) -> list[RunningModel]:
        return [RunningModel(
            name=model.model_ref, provider=self.name,
            details={"source": "/v1/models", "confidence": "inferred"},
        ) for model in self.list_models()]

    def show_model(self, model: str) -> dict[str, Any]:
        if not model:
            raise ValueError("模型名称不能为空")
        known = {item.model_ref for item in self.list_models()}
        if model not in known:
            raise ProviderAPIError(f"llama.cpp 当前服务未报告模型 {model}", provider=self.name)
        payload = self._client.request_json("GET", "/props")
        return {"model": model, "props": payload, "confidence": "exact"}

    def start_model(
        self,
        model_path: str | os.PathLike[str],
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        extra_args: Sequence[str] = (),
        context_length: int | None = None,
        gpu_layers: int | None = None,
    ) -> ManagedProcess:
        path = Path(model_path).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() != ".gguf":
            raise UnsupportedOperationError(f"llama-server 只能直接装入真实存在的 GGUF 文件：{path}")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("llama-server 仅允许绑定本机回环地址")
        if not 1 <= int(port) <= 65535:
            raise ValueError("服务端口必须在 1 到 65535 之间")
        executable = self.server_executable
        if not executable:
            raise BackendNotInstalledError("未检测到 llama-server，不能启动裸 GGUF；请先安装或配置真实路径。")
        command = list(self._launcher) if self._launcher else [executable]
        if self._launcher:
            command[0] = executable
        command += ["--model", str(path), "--host", host, "--port", str(int(port))]
        if context_length is not None:
            if int(context_length) <= 0:
                raise ValueError("上下文长度必须大于 0")
            command += ["--ctx-size", str(int(context_length))]
        if gpu_layers is not None:
            command += ["--n-gpu-layers", str(int(gpu_layers))]
        command += [str(arg) for arg in extra_args]
        return self._supervisor.start(
            command, provider=self.name, model_id=str(path), purpose="llama-server",
        )

    start_server = start_model
    load_model = start_model

    def stop_model(self, process: int | str | ManagedProcess, *, timeout: float = 5.0) -> bool:
        return self._supervisor.stop(process, timeout=timeout)

    def quantize(
        self,
        input_path: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        quantization: str,
        *,
        threads: int | None = None,
    ) -> ManagedProcess:
        source = Path(input_path).expanduser().resolve()
        target = Path(output_path).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() != ".gguf":
            raise ValueError(f"量化输入必须是真实 GGUF 文件：{source}")
        if not re.fullmatch(r"[A-Za-z0-9_]+", quantization or ""):
            raise ValueError("量化类型只能包含字母、数字和下划线")
        if source == target:
            raise ValueError("量化输出不能覆盖输入文件")
        executable = self.quantizer_executable
        if not executable:
            raise BackendNotInstalledError("未检测到 llama-quantize/quantize，未执行伪造转换。")
        command = [executable, str(source), str(target), quantization]
        if threads is not None:
            if int(threads) <= 0:
                raise ValueError("量化线程数必须大于 0")
            command.append(str(int(threads)))
        target.parent.mkdir(parents=True, exist_ok=True)
        return self._supervisor.start(
            command, provider=self.name, model_id=str(source), purpose="quantize",
        )

    def close(self) -> None:
        if self._owns_supervisor:
            self._supervisor.close()
