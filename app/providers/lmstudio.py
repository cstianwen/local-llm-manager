"""LM Studio 本机 REST API、安装检测与显式启动器。"""
from __future__ import annotations

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


def _lm_studio_executable() -> str:
    found = find_executable(None, "lms", "lms.exe")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    candidates = [
        Path(local) / "Programs" / "LM Studio" / "LM Studio.exe",
        Path(local) / "LM Studio" / "LM Studio.exe",
    ] if local else []
    return next((str(path) for path in candidates if path.is_file()), "")


class LMStudioProvider(Provider):
    name = "lmstudio"
    display_name = "LM Studio"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234",
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
        return find_executable(self._executable_path) if self._executable_path else _lm_studio_executable()

    def _native_models(self) -> dict[str, Any]:
        payload = self._client.request_json("GET", "/api/v0/models")
        if not isinstance(payload, dict):
            raise ProviderAPIError("LM Studio /api/v0/models 响应不是对象", provider=self.name)
        return payload

    def detect(self) -> ProviderStatus:
        executable = self.executable
        installed = bool(executable)
        try:
            payload = self._native_models()
            api = "native"
        except ProviderAPIError as native_error:
            try:
                payload = self._client.request_json("GET", "/v1/models")
                api = "openai"
            except BackendOfflineError as exc:
                self._last_status = ProviderStatus(
                    provider=self.name, installed=installed, running=False,
                    state=CapabilityState.NOT_RUNNING if installed else CapabilityState.NOT_INSTALLED,
                    executable=executable, base_url=self.base_url,
                    message="未检测到运行中的 LM Studio 本机服务。", error=str(exc),
                )
                return self._last_status
            except ProviderAPIError:
                self._last_status = ProviderStatus(
                    provider=self.name, installed=installed, running=False,
                    state=CapabilityState.NOT_RUNNING if installed else CapabilityState.NOT_INSTALLED,
                    executable=executable, base_url=self.base_url,
                    message="本机地址有响应，但不是可识别的 LM Studio API。",
                    error=str(native_error),
                )
                return self._last_status
        except BackendOfflineError as exc:
            self._last_status = ProviderStatus(
                provider=self.name, installed=installed, running=False,
                state=CapabilityState.NOT_RUNNING if installed else CapabilityState.NOT_INSTALLED,
                executable=executable, base_url=self.base_url,
                message=(
                    "已找到 LM Studio，但本机服务未运行。" if installed else
                    "未找到 LM Studio/lms，且本机服务不可访问。"
                ), error=str(exc),
            )
            return self._last_status
        self._last_status = ProviderStatus(
            provider=self.name, installed=True, running=True,
            state=CapabilityState.SUPPORTED, executable=executable,
            base_url=self.base_url, message="LM Studio 本机服务可用。",
            details={"api": api, "response": payload},
        )
        return self._last_status

    def capabilities(self) -> dict[str, Capability]:
        status = self._last_status or self.detect()
        running = status.running
        state = CapabilityState.SUPPORTED if running else status.state
        native = status.details.get("api") == "native"
        return {
            "detect": Capability(CapabilityState.SUPPORTED),
            "list": Capability(state, status.message),
            "ps": Capability(
                CapabilityState.SUPPORTED if running and native else
                (CapabilityState.UNSUPPORTED if running else state),
                "原生 API 可区分已装入模型。" if native else "OpenAI /v1/models 不能证明模型已装入。",
            ),
            "load": Capability(
                CapabilityState.SUPPORTED if running and native else
                (CapabilityState.UNSUPPORTED if running else state),
                "使用 LM Studio 原生 /api/v0/models/load。" if native else "需要 LM Studio 原生 API。",
            ),
            "unload": Capability(
                CapabilityState.SUPPORTED if running and native else
                (CapabilityState.UNSUPPORTED if running else state),
                "使用 LM Studio 原生 /api/v0/models/unload。" if native else "需要 LM Studio 原生 API。",
            ),
            "start_service": Capability(
                CapabilityState.SUPPORTED if self._launcher else CapabilityState.UNSUPPORTED,
                "仅使用用户显式配置的启动器。" if self._launcher else "未配置 LM Studio 服务启动器。",
            ),
            "pull": Capability(CapabilityState.UNSUPPORTED, "本 Provider 不伪造 LM Studio 下载进度。"),
            "delete": Capability(CapabilityState.UNSUPPORTED, "本 Provider 不删除 LM Studio 模型文件。"),
        }

    @staticmethod
    def _rows(payload: dict[str, Any], native: bool) -> list[dict[str, Any]]:
        rows = payload.get("models") if native else payload.get("data")
        if rows is None:
            rows = payload.get("data", [])
        if not isinstance(rows, list):
            raise ProviderAPIError("LM Studio 模型响应缺少数组", provider="lmstudio", payload=payload)
        return [item for item in rows if isinstance(item, dict)]

    def _list(self, native: bool = True) -> tuple[list[ModelInfo], list[dict[str, Any]]]:
        payload = self._native_models() if native else self._client.request_json("GET", "/v1/models")
        rows = self._rows(payload, native)
        models = []
        for item in rows:
            name = str(item.get("id") or item.get("key") or item.get("model") or item.get("path") or "")
            if not name:
                continue
            path = str(item.get("path") or "")
            models.append(ModelInfo(
                file_path=path or f"lmstudio://{name}", file_name=Path(path).name if path else name,
                format="gguf" if path.lower().endswith(".gguf") else "unknown",
                size_bytes=max(0, int(item.get("size_bytes") or item.get("size") or 0)),
                architecture=str(item.get("architecture") or ""),
                params_b=0.0, quant=str(item.get("quantization") or ""),
                provider=self.name, model_ref=name, model_id=f"lmstudio:{name}",
                display_name=str(item.get("display_name") or name), source_type="backend",
            ))
        return models, rows

    def list_models(self) -> list[ModelInfo]:
        try:
            return self._list(True)[0]
        except ProviderAPIError:
            return self._list(False)[0]

    def running_models(self) -> list[RunningModel]:
        _, rows = self._list(True)
        result = []
        for item in rows:
            instances = item.get("loaded_instances")
            loaded = bool(instances) or bool(item.get("loaded")) or str(item.get("state", "")).lower() == "loaded"
            if not loaded:
                continue
            name = str(item.get("id") or item.get("key") or item.get("path") or "")
            result.append(RunningModel(
                name=name, provider=self.name,
                size_bytes=max(0, int(item.get("size_bytes") or item.get("size") or 0)),
                details={**item, "confidence": "exact"},
            ))
        return result

    def show_model(self, model: str) -> dict[str, Any]:
        _, rows = self._list(True)
        for item in rows:
            names = {item.get("id"), item.get("key"), item.get("model"), item.get("path")}
            if model in names:
                return item
        raise ProviderAPIError(f"LM Studio 未报告模型 {model}", provider=self.name)

    def load_model(self, model: str, **options: Any) -> OperationResult:
        if not model:
            raise ValueError("模型标识不能为空")
        response = self._client.request_json(
            "POST", "/api/v0/models/load", {"model": model, **options},
            timeout=max(self._client.timeout, 120.0),
        )
        return OperationResult(True, f"LM Studio 已接受模型 {model} 的装入请求。",
                                 data=response if isinstance(response, dict) else {"response": response})

    def unload_model(self, model: str, **options: Any) -> OperationResult:
        instance_id = str(options.get("instance_id") or model)
        if not instance_id:
            raise ValueError("模型实例标识不能为空")
        response = self._client.request_json(
            "POST", "/api/v0/models/unload", {"instance_id": instance_id},
        )
        return OperationResult(True, f"LM Studio 已接受实例 {instance_id} 的卸载请求。",
                                 data=response if isinstance(response, dict) else {"response": response})

    def start_server(self, *, extra_args: Sequence[str] = ()) -> ManagedProcess:
        if not self._launcher:
            raise UnsupportedOperationError("未配置 LM Studio 启动器；不会猜测命令或伪造服务启动。")
        executable = find_executable(self._launcher[0])
        if not executable:
            raise BackendNotInstalledError(f"配置的 LM Studio 启动器不存在：{self._launcher[0]}")
        command = [executable, *self._launcher[1:], *[str(arg) for arg in extra_args]]
        return self._supervisor.start(command, provider=self.name, purpose="service-launcher")

    def close(self) -> None:
        if self._owns_supervisor:
            self._supervisor.close()
