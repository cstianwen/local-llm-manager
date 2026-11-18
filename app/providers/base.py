"""Provider 统一数据结构、能力状态与本机 HTTP 基础设施。"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..core.errors import (
    BackendNotInstalledError,
    BackendOfflineError,
    ModelManagerError,
    OperationCancelledError,
    UnsupportedOperationError,
)
from ..core.models import DictSerializable, DownloadProgress, ModelInfo, OperationResult, RunningModel


class CapabilityState(str, Enum):
    """能力状态比简单的布尔值更能表达可执行的降级原因。"""

    SUPPORTED = "supported"
    NOT_INSTALLED = "not_installed"
    NOT_RUNNING = "not_running"
    ELEVATION_REQUIRED = "elevation_required"
    UNSUPPORTED = "unsupported"
    NETWORK_REQUIRED = "network_required"


@dataclass
class Capability(DictSerializable):
    state: CapabilityState
    reason: str = ""
    action: str = ""

    @property
    def supported(self) -> bool:
        return self.state in {CapabilityState.SUPPORTED, CapabilityState.NETWORK_REQUIRED}


@dataclass
class ProviderStatus(DictSerializable):
    provider: str
    installed: bool = False
    running: bool = False
    state: CapabilityState = CapabilityState.NOT_INSTALLED
    version: str = ""
    executable: str = ""
    base_url: str = ""
    message: str = ""
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ProviderAPIError(ModelManagerError):
    """本机服务已响应，但 HTTP 或后端 API 返回了错误。"""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int = 0,
        payload: Any = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.payload = payload


CancellationToken = Any
ProgressCallback = Callable[[DownloadProgress], None]


def check_cancelled(token: CancellationToken | None) -> None:
    """兼容 core.tasks.CancellationToken，同时避免在 Provider 中引入 Qt。"""
    if token is None:
        return
    raise_if_cancelled = getattr(token, "raise_if_cancelled", None)
    if callable(raise_if_cancelled):
        raise_if_cancelled()
        return
    is_cancelled = getattr(token, "is_cancelled", None)
    cancelled = is_cancelled() if callable(is_cancelled) else getattr(token, "cancelled", False)
    if cancelled:
        raise OperationCancelledError("任务已取消")


def find_executable(
    explicit: str | os.PathLike[str] | None = None,
    *names: str,
) -> str:
    """只返回真实存在的可执行文件；显式路径无效时不伪装为已安装。"""
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(os.fspath(explicit))
        return found or ""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def validate_loopback_url(base_url: str) -> str:
    """限制 Provider 只能访问本机回环地址，拒绝云服务和局域网目标。"""
    value = (base_url or "").strip().rstrip("/")
    if "://" not in value:
        value = "http://" + value
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    is_loopback = host.lower() == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if parsed.scheme.lower() != "http" or not is_loopback:
        raise ValueError(f"只允许 HTTP 本机回环地址，已拒绝：{base_url}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("本机服务地址不能包含凭据、查询参数或片段")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"服务端口无效：{base_url}") from exc
    return value


def _api_error(payload: Any, provider: str, status_code: int = 0) -> ProviderAPIError | None:
    if not isinstance(payload, dict) or not payload.get("error"):
        return None
    detail = payload["error"]
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False)
    return ProviderAPIError(
        f"{provider or '后端'} API 错误：{detail}",
        provider=provider,
        status_code=status_code,
        payload=payload,
    )


class LoopbackHTTPClient:
    """仅使用标准库的 JSON/NDJSON 客户端，超时和错误语义在各 Provider 间一致。"""

    def __init__(self, provider: str, base_url: str, timeout: float = 10.0):
        if timeout <= 0:
            raise ValueError("HTTP 超时必须大于 0 秒")
        self.provider = provider
        self.base_url = validate_loopback_url(base_url)
        self.timeout = float(timeout)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    @staticmethod
    def _body(payload: Any) -> bytes | None:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _request(self, method: str, path: str, payload: Any = None) -> urllib.request.Request:
        body = self._body(payload)
        headers = {"Accept": "application/json", "User-Agent": "LocalModelManager/1.0"}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return urllib.request.Request(
            self._url(path), data=body, headers=headers, method=method.upper()
        )

    def _http_error(self, exc: urllib.error.HTTPError) -> ProviderAPIError:
        try:
            raw = exc.read(2 * 1024 * 1024)
            text = raw.decode("utf-8", errors="replace")
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = {"error": text} if text else {}
        except Exception:
            payload = {}
        error = _api_error(payload, self.provider, exc.code)
        if error is not None:
            return error
        reason = exc.reason or "未知 HTTP 错误"
        return ProviderAPIError(
            f"{self.provider} HTTP {exc.code}：{reason}",
            provider=self.provider,
            status_code=exc.code,
            payload=payload,
        )

    def _offline_error(self, exc: BaseException) -> BackendOfflineError:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            detail = "连接或读取超时"
        else:
            detail = str(reason) or exc.__class__.__name__
        return BackendOfflineError(
            f"无法访问本机 {self.provider} 服务 {self.base_url}：{detail}。"
            "请确认服务已运行；本工具不会自动安装或启动它。"
        )

    def request_json(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        timeout: float | None = None,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> Any:
        request = self._request(method, path, payload)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise ProviderAPIError(
                        f"{self.provider} 响应超过安全限制（{max_bytes} 字节）",
                        provider=self.provider,
                    )
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            raise self._offline_error(exc) from exc
        if not raw.strip():
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProviderAPIError(
                f"{self.provider} 返回了无效 JSON：{exc}", provider=self.provider
            ) from exc
        error = _api_error(result, self.provider)
        if error is not None:
            raise error
        return result

    def stream_ndjson(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        timeout: float | None = None,
        cancel_token: CancellationToken | None = None,
        max_line_bytes: int = 4 * 1024 * 1024,
    ) -> Iterator[dict[str, Any]]:
        """逐行产生真实响应；取消在每次读取前后检查，响应总会关闭。"""
        request = self._request(method, path, payload)
        response = None
        try:
            check_cancelled(cancel_token)
            response = urllib.request.urlopen(request, timeout=timeout or self.timeout)
            while True:
                check_cancelled(cancel_token)
                try:
                    line = response.readline(max_line_bytes + 1)
                except (TimeoutError, socket.timeout) as exc:
                    raise BackendOfflineError(
                        f"读取 {self.provider} 流式响应超时；任务未显示为成功。"
                    ) from exc
                if not line:
                    break
                if len(line) > max_line_bytes:
                    raise ProviderAPIError(
                        f"{self.provider} 单行流式响应超过安全限制",
                        provider=self.provider,
                    )
                if not line.strip():
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ProviderAPIError(
                        f"{self.provider} 返回了无效 NDJSON：{exc}", provider=self.provider
                    ) from exc
                if not isinstance(event, dict):
                    raise ProviderAPIError(
                        f"{self.provider} NDJSON 事件不是对象", provider=self.provider
                    )
                error = _api_error(event, self.provider)
                if error is not None:
                    raise error
                yield event
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise self._offline_error(exc) from exc
        finally:
            if response is not None:
                response.close()


class Provider(ABC):
    """所有后端的窄接口；不支持的操作必须抛出带中文原因的异常。"""

    name = ""
    display_name = ""

    @abstractmethod
    def detect(self) -> ProviderStatus:
        raise NotImplementedError

    def status(self) -> ProviderStatus:
        return self.detect()

    @abstractmethod
    def capabilities(self) -> dict[str, Capability]:
        raise NotImplementedError

    def capability_matrix(self) -> dict[str, Capability]:
        return self.capabilities()

    def _unsupported(self, operation: str, reason: str = "") -> None:
        suffix = f"：{reason}" if reason else ""
        raise UnsupportedOperationError(f"{self.display_name or self.name} 不支持{operation}{suffix}")

    def list_models(self) -> list[ModelInfo]:
        self._unsupported("模型列表")

    def running_models(self) -> list[RunningModel]:
        self._unsupported("运行中模型查询")

    def show_model(self, model: str) -> dict[str, Any]:
        self._unsupported("模型详情查询")

    def load_model(self, model: str, **options: Any) -> OperationResult:
        self._unsupported("模型装入")

    def unload_model(self, model: str, **options: Any) -> OperationResult:
        self._unsupported("模型卸载")

    def pull_model(
        self,
        model: str,
        on_progress: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> Iterator[DownloadProgress]:
        self._unsupported("模型下载")

    def delete_model(self, model: str) -> OperationResult:
        self._unsupported("模型删除")


# 兼容调用方常用的语义化基类名称。
BaseProvider = Provider
