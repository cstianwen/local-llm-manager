"""vLLM 本机 OpenAI 兼容服务检测与真实启动器。"""
from __future__ import annotations

import importlib.util
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..core.errors import BackendOfflineError, BackendNotInstalledError, UnsupportedOperationError
from ..core.models import ModelInfo, OperationResult, RunningModel
from ..core.processes import ManagedProcess, ProcessSupervisor
from .base import (
    Capability, CapabilityState, LoopbackHTTPClient, Provider,
    ProviderAPIError, ProviderStatus, find_executable,
)


class VLLMProvider(Provider):
    name = "vllm"
    display_name = "vLLM"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *, timeout: float = 5.0,
        executable_path: str | os.PathLike[str] | None = None,
        launcher: Sequence[str] | None = None,
        supervisor: ProcessSupervisor | None = None,
    ):
        self._client = LoopbackHTTPClient(self.name, base_url, timeout)
        self.base_url = self._client.base_url
        self._executable_path = executable_path
        self._launcher = tuple(launcher) if launcher else ()
        self._supervisor = supervisor or ProcessSupervisor()
        self._owns_supervisor = supervisor is None
        self._last_status: ProviderStatus | None = None

    @property
    def executable(self) -> str:
        if self._launcher:
            return find_executable(self._launcher[0])
        return find_executable(self._executable_path, "vllm", "vllm.exe")

    @staticmethod
    def _python_module_installed() -> bool:
        try:
            return importlib.util.find_spec("vllm") is not None
        except (ImportError, ValueError):
            return False

    def _models_payload(self) -> dict[str, Any]:
        payload = self._client.request_json("GET", "/v1/models")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderAPIError("vLLM /v1/models 响应缺少 data 数组", provider=self.name)
        return payload

    def detect(self) -> ProviderStatus:
        executable = self.executable
        installed = bool(executable) or self._python_module_installed()
        try:
            payload = self._models_payload()
            version = ""
            try:
                version_payload = self._client.request_json("GET", "/version")
                version = str(version_payload.get("version", "")) if isinstance(version_payload, dict) else ""
            except ProviderAPIError:
                pass  # 旧版本没有 /version，不影响已确认的 /v1/models 能力。
        except BackendOfflineError as exc:
            status = ProviderStatus(
                provider=self.name, installed=installed, running=False,
                state=CapabilityState.NOT_RUNNING if installed else CapabilityState.NOT_INSTALLED,
                executable=executable, base_url=self.base_url,
                message=(
                    "已检测到 vLLM，但本机服务未运行。" if installed else
                    "未检测到 vLLM 可执行文件/Python 模块，且本机服务不可访问。"
                ), error=str(exc),
            )
        except ProviderAPIError as exc:
            status = ProviderStatus(
                provider=self.name, installed=installed, running=False,
                state=CapabilityState.NOT_RUNNING if installed else CapabilityState.NOT_INSTALLED,
                executable=executable, base_url=self.base_url,
                message="本机地址有响应，但不能确认是兼容的 vLLM 服务。", error=str(exc),
            )
        else:
            status = ProviderStatus(
                provider=self.name, installed=True, running=True,
                state=CapabilityState.SUPPORTED, version=version, executable=executable,
                base_url=self.base_url, message="vLLM 本机服务可用。",
                details={"models_response": payload},
            )
        self._last_status = status
        return status

    def capabilities(self) -> dict[str, Capability]:
        status = self._last_status or self.detect()
        state = CapabilityState.SUPPORTED if status.running else status.state
        can_start = bool(self.executable)
        module_only = self._python_module_installed() and not can_start
        return {
            "detect": Capability(CapabilityState.SUPPORTED),
            "list": Capability(state, status.message),
            "ps": Capability(state, "vLLM 服务报告的当前模型。" if status.running else status.message),
            "load": Capability(
                CapabilityState.SUPPORTED if can_start else
                (CapabilityState.UNSUPPORTED if module_only else CapabilityState.NOT_INSTALLED),
                (
                    "可启动本地模型目录；模型标识下载不在此处伪造。" if can_start else
                    "检测到 vLLM Python 模块，但没有可调用的 vllm CLI；不会猜测启动方式。"
                    if module_only else "未检测到 vLLM。"
                ),
            ),
            "unload": Capability(state, "只能停止由本工具创建的 vLLM 进程。"),
            "start_service": Capability(
                CapabilityState.SUPPORTED if self._launcher else state,
                "已配置显式启动器。" if self._launcher else status.message,
            ),
            "pull": Capability(CapabilityState.NETWORK_REQUIRED, "vLLM 下载由模型仓库与运行环境负责，本 Provider 不显示伪进度。"),
            "delete": Capability(CapabilityState.UNSUPPORTED, "vLLM Provider 不删除模型文件。"),
            "quantize": Capability(CapabilityState.UNSUPPORTED, "不在此处调用未经检测的 vLLM 量化工具。"),
        }

    def list_models(self) -> list[ModelInfo]:
        result = []
        for item in self._models_payload()["data"]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            name = str(item["id"])
            path = str(item.get("root") or item.get("path") or "")
            result.append(ModelInfo(
                file_path=path or f"vllm://{name}", file_name=Path(path).name if path else name,
                format="safetensors" if path else "unknown", size_bytes=0,
                provider=self.name, model_ref=name, model_id=f"vllm:{name}",
                display_name=name, source_type="backend",
            ))
        return result

    def running_models(self) -> list[RunningModel]:
        return [RunningModel(
            name=model.model_ref, provider=self.name,
            details={"source": "/v1/models", "confidence": "exact"},
        ) for model in self.list_models()]

    def show_model(self, model: str) -> dict[str, Any]:
        for item in self._models_payload()["data"]:
            if isinstance(item, dict) and item.get("id") == model:
                return item
        raise ProviderAPIError(f"vLLM 当前服务未报告模型 {model}", provider=self.name)

    def start_model(
        self,
        model_path: str | os.PathLike[str],
        *, host: str = "127.0.0.1", port: int = 8000,
        extra_args: Sequence[str] = (),
    ) -> ManagedProcess:
        path = Path(model_path).expanduser().resolve()
        if not path.exists():
            raise UnsupportedOperationError(
                f"vLLM 启动要求真实本地模型目录/文件：{path}；不会把远程模型标识伪装成本地装入。"
            )
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("vLLM 仅允许绑定本机回环地址")
        if not 1 <= int(port) <= 65535:
            raise ValueError("服务端口必须在 1 到 65535 之间")
        if self._launcher:
            executable = find_executable(self._launcher[0])
            if not executable:
                raise BackendNotInstalledError(f"配置的 vLLM 启动器不存在：{self._launcher[0]}")
            command = [executable, *self._launcher[1:], str(path)]
        elif self.executable:
            command = [self.executable, "serve", str(path)]
        else:
            raise BackendNotInstalledError("未检测到 vLLM 可执行文件；请安装或配置显式启动器。")
        command += ["--host", host, "--port", str(int(port)), *[str(arg) for arg in extra_args]]
        return self._supervisor.start(command, provider=self.name, model_id=str(path), purpose="vllm-server")

    start_server = start_model
    load_model = start_model

    def stop_model(self, process: int | str | ManagedProcess, *, timeout: float = 5.0) -> bool:
        return self._supervisor.stop(process, timeout=timeout)

    def unload_model(self, model: str, **options: Any) -> OperationResult:
        process = options.get("process")
        if process is None:
            raise UnsupportedOperationError("vLLM 没有统一热卸载 API；请传入本工具创建的 process 句柄以停止服务。")
        stopped = self._supervisor.stop(process)
        return OperationResult(True, "已停止本工具创建的 vLLM 进程。" if stopped else "该受管进程已经退出。",
                               data={"stopped": stopped})

    def close(self) -> None:
        if self._owns_supervisor:
            self._supervisor.close()
