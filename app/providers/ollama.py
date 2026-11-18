"""只访问本机既有 Ollama 服务的 Provider。"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ..core.errors import BackendOfflineError
from ..core.models import DownloadProgress, ModelInfo, OperationResult, RunningModel
from .base import (
    CancellationToken,
    Capability,
    CapabilityState,
    LoopbackHTTPClient,
    ProgressCallback,
    Provider,
    ProviderAPIError,
    ProviderStatus,
    check_cancelled,
    find_executable,
)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


def _parameter_billions(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().upper().replace(" ", "")
    try:
        if text.endswith("B"):
            return float(text[:-1])
        if text.endswith("M"):
            return float(text[:-1]) / 1000.0
        if text.endswith("K"):
            return float(text[:-1]) / 1_000_000.0
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


def _ollama_executable() -> str:
    found = find_executable(None, "ollama", "ollama.exe")
    if found:
        return found
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "Ollama" / "ollama.exe")
    return next((str(path) for path in candidates if path.is_file()), "")


class OllamaProvider(Provider):
    name = "ollama"
    display_name = "Ollama"

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        *,
        timeout: float = 10.0,
        stream_timeout: float = 60.0,
        executable_path: str | os.PathLike[str] | None = None,
    ):
        self._client = LoopbackHTTPClient(self.name, base_url, timeout)
        self.base_url = self._client.base_url
        self.stream_timeout = float(stream_timeout)
        if self.stream_timeout <= 0:
            raise ValueError("流式读取超时必须大于 0 秒")
        self._explicit_executable = executable_path
        self._last_status: ProviderStatus | None = None

    def detect(self) -> ProviderStatus:
        executable = find_executable(self._explicit_executable) if self._explicit_executable else _ollama_executable()
        try:
            payload = self.version()
            if not payload.get("version"):
                raise ProviderAPIError("本机 /api/version 响应缺少 version，不能确认是 Ollama。", provider=self.name)
        except (BackendOfflineError, ProviderAPIError) as exc:
            installed = bool(executable)
            status = ProviderStatus(
                provider=self.name,
                installed=installed,
                running=False,
                state=CapabilityState.NOT_RUNNING if installed else CapabilityState.NOT_INSTALLED,
                executable=executable,
                base_url=self.base_url,
                message=(
                    "已找到 Ollama，但本机服务未运行或 API 不兼容。请检查本机 Ollama 后重试。"
                    if installed else
                    "本机地址不可访问或不是可识别的 Ollama，且未找到 Ollama 可执行文件。"
                    "本工具不会自动安装或启动 Ollama。"
                ),
                error=str(exc),
            )
        else:
            status = ProviderStatus(
                provider=self.name,
                installed=True,
                running=True,
                state=CapabilityState.SUPPORTED,
                version=str(payload.get("version", "")),
                executable=executable,
                base_url=self.base_url,
                message="本机 Ollama 服务可用。",
                details={"version_response": payload},
            )
        self._last_status = status
        return status

    def capabilities(self) -> dict[str, Capability]:
        status = self._last_status or self.detect()
        if not status.installed:
            unavailable = Capability(
                CapabilityState.NOT_INSTALLED,
                "未检测到 Ollama 安装或正在运行的本机服务。",
                "安装并自行启动 Ollama 后重试；本工具不会自动安装。",
            )
            capabilities = {
                name: unavailable for name in
                ("detect", "list", "show", "ps", "load", "unload", "pull", "delete", "chat", "generate")
            }
            capabilities["detect"] = Capability(CapabilityState.SUPPORTED, "可检测安装与本机服务状态。")
        elif not status.running:
            unavailable = Capability(
                CapabilityState.NOT_RUNNING,
                status.message or "Ollama 服务未运行。",
                "请自行启动本机 Ollama 服务。",
            )
            capabilities = {
                name: unavailable for name in
                ("list", "show", "ps", "load", "unload", "pull", "delete", "chat", "generate")
            }
            capabilities["detect"] = Capability(CapabilityState.SUPPORTED)
        else:
            capabilities = {
                name: Capability(CapabilityState.SUPPORTED)
                for name in ("detect", "list", "show", "ps", "load", "unload", "delete", "chat", "generate")
            }
            capabilities["pull"] = Capability(
                CapabilityState.NETWORK_REQUIRED,
                "拉取模型由本机 Ollama 执行，通常还需要访问模型仓库；离线时可能失败。",
                "确认网络与 Ollama 模型仓库可用，下载失败会原样报告。",
            )
        capabilities["install"] = Capability(
            CapabilityState.UNSUPPORTED, "本工具不自动安装 Ollama。", "请使用 Ollama 官方安装包。"
        )
        capabilities["start_service"] = Capability(
            CapabilityState.UNSUPPORTED, "本工具不自动启动 Ollama。", "请由用户自行启动本机服务。"
        )
        return capabilities

    def version(self) -> dict[str, Any]:
        result = self._client.request_json("GET", "/api/version")
        if not isinstance(result, dict):
            raise ProviderAPIError("Ollama version 响应不是对象", provider=self.name)
        return result

    def list_models(self) -> list[ModelInfo]:
        payload = self._client.request_json("GET", "/api/tags")
        if not isinstance(payload, dict) or not isinstance(payload.get("models", []), list):
            raise ProviderAPIError("Ollama tags 响应缺少 models 数组", provider=self.name, payload=payload)
        models = []
        for item in payload.get("models", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("model") or "")
            if not name:
                continue
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            digest = str(item.get("digest") or "")
            model_id = f"ollama:{digest or name}"
            models.append(ModelInfo(
                file_path=f"ollama://{name}",
                file_name=name,
                format="ollama",
                size_bytes=max(0, int(item.get("size") or 0)),
                architecture=str(details.get("family") or details.get("architecture") or ""),
                params_b=_parameter_billions(details.get("parameter_size")),
                quant=str(details.get("quantization_level") or ""),
                provider=self.name,
                model_ref=name,
                model_id=model_id,
                display_name=name,
                source_type="backend",
                integrity="unknown",
            ))
        return models

    tags = list_models

    def running_models(self) -> list[RunningModel]:
        payload = self._client.request_json("GET", "/api/ps")
        if not isinstance(payload, dict) or not isinstance(payload.get("models", []), list):
            raise ProviderAPIError("Ollama ps 响应缺少 models 数组", provider=self.name, payload=payload)
        result = []
        for item in payload.get("models", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("model") or "")
            if not name:
                continue
            result.append(RunningModel(
                name=name,
                provider=self.name,
                size_bytes=max(0, int(item.get("size") or 0)),
                size_vram=max(0, int(item.get("size_vram") or 0)),
                expires_at=str(item.get("expires_at") or ""),
                details=item,
            ))
        return result

    ps = running_models

    def show_model(self, model: str) -> dict[str, Any]:
        self._require_model(model)
        payload = self._client.request_json("POST", "/api/show", {"model": model})
        if not isinstance(payload, dict):
            raise ProviderAPIError("Ollama show 响应不是对象", provider=self.name)
        return payload

    show = show_model

    @staticmethod
    def _require_model(model: str) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("模型名称不能为空")

    def _set_keep_alive(self, model: str, keep_alive: Any) -> OperationResult:
        self._require_model(model)
        response = self._client.request_json(
            "POST",
            "/api/generate",
            {"model": model, "prompt": "", "keep_alive": keep_alive, "stream": False},
            timeout=max(self._client.timeout, 120.0),
        )
        if not isinstance(response, dict):
            raise ProviderAPIError("Ollama keep_alive 响应不是对象", provider=self.name)
        if response.get("done") is False:
            raise ProviderAPIError(
                f"Ollama 未确认 keep_alive 操作：{response.get('done_reason') or '未知原因'}",
                provider=self.name,
                payload=response,
            )
        return OperationResult(True, "Ollama 已确认 keep_alive 请求。", data=response)

    def load_model(self, model: str, *, keep_alive: Any = "5m", **options: Any) -> OperationResult:
        result = self._set_keep_alive(model, keep_alive)
        result.message = f"模型 {model} 的装入请求已由 Ollama 确认，keep_alive={keep_alive!r}。"
        return result

    def unload_model(self, model: str, **options: Any) -> OperationResult:
        result = self._set_keep_alive(model, 0)
        result.message = f"模型 {model} 的卸载请求已由 Ollama 确认。"
        return result

    def keep_alive(self, model: str, duration: Any = "5m") -> OperationResult:
        if duration == 0 or duration == "0" or duration == "0s":
            return self.unload_model(model)
        return self.load_model(model, keep_alive=duration)

    def pull_model(
        self,
        model: str,
        on_progress: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> Iterator[DownloadProgress]:
        self._require_model(model)
        started = time.monotonic()
        last_time = started
        last_bytes = 0
        succeeded = False
        last = DownloadProgress(model_id=model, status="pending")
        for event in self._client.stream_ndjson(
            "POST",
            "/api/pull",
            {"model": model, "stream": True},
            timeout=self.stream_timeout,
            cancel_token=cancel_token,
        ):
            check_cancelled(cancel_token)
            now = time.monotonic()
            downloaded = max(0, int(event.get("completed") or 0))
            total = max(0, int(event.get("total") or 0))
            elapsed = max(1e-9, now - last_time)
            speed = max(0.0, (downloaded - last_bytes) / elapsed)
            if downloaded < last_bytes:
                speed = 0.0  # 不同 layer 的计数可能重置，不能显示负速度。
            text = str(event.get("status") or "downloading")
            succeeded = text.lower() == "success"
            last = DownloadProgress(
                model_id=model,
                downloaded_bytes=downloaded,
                total_bytes=total,
                speed_bps=speed,
                status="completed" if succeeded else "downloading",
            )
            if on_progress is not None:
                on_progress(last)
            yield last
            last_time, last_bytes = now, downloaded
        if not succeeded:
            raise ProviderAPIError(
                f"Ollama pull 流在收到 success 之前结束，最后状态为 {last.status!r}；不能视为下载成功。",
                provider=self.name,
            )

    pull = pull_model

    def delete_model(self, model: str) -> OperationResult:
        self._require_model(model)
        response = self._client.request_json("DELETE", "/api/delete", {"model": model})
        return OperationResult(
            True,
            f"Ollama 已接受并处理模型 {model} 的删除请求。",
            data=response if isinstance(response, dict) else {"response": response},
        )

    delete = delete_model

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        options: dict[str, Any] | None = None,
        keep_alive: Any = None,
        cancel_token: CancellationToken | None = None,
    ) -> Iterator[dict[str, Any]]:
        self._require_model(model)
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": True}
        if options:
            payload["options"] = options
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        yield from self._client.stream_ndjson(
            "POST", "/api/generate", payload,
            timeout=self.stream_timeout, cancel_token=cancel_token,
        )

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        keep_alive: Any = None,
        cancel_token: CancellationToken | None = None,
    ) -> Iterator[dict[str, Any]]:
        self._require_model(model)
        if not isinstance(messages, list):
            raise ValueError("messages 必须是消息对象数组")
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if options:
            payload["options"] = options
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        yield from self._client.stream_ndjson(
            "POST", "/api/chat", payload,
            timeout=self.stream_timeout, cancel_token=cancel_token,
        )
