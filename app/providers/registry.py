"""Provider 注册表：统一状态轮询与能力矩阵。"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from ..core.processes import ProcessSupervisor
from .base import Capability, CapabilityState, Provider, ProviderStatus
from .llamacpp import LlamaCppProvider
from .lmstudio import LMStudioProvider
from .ollama import OllamaProvider
from .vllm import VLLMProvider


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider] | None = None):
        self._providers: dict[str, Provider] = {}
        self._owned_supervisor: ProcessSupervisor | None = None
        for provider in providers or ():
            self.register(provider)

    def register(self, provider: Provider, *, replace: bool = False) -> Provider:
        if not provider.name:
            raise ValueError("Provider 必须具有非空 name")
        if provider.name in self._providers and not replace:
            raise ValueError(f"Provider 已注册：{provider.name}")
        self._providers[provider.name] = provider
        return provider

    def unregister(self, name: str) -> Provider:
        try:
            return self._providers.pop(name)
        except KeyError as exc:
            raise KeyError(f"未注册 Provider：{name}") from exc

    def get(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"未注册 Provider：{name}") from exc

    get_provider = get

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(self._providers.values())

    def __iter__(self) -> Iterator[Provider]:
        return iter(self._providers.values())

    def __len__(self) -> int:
        return len(self._providers)

    def detect_all(self) -> dict[str, ProviderStatus]:
        result = {}
        for name, provider in self._providers.items():
            try:
                result[name] = provider.detect()
            except Exception as exc:
                result[name] = ProviderStatus(
                    provider=name,
                    state=CapabilityState.NOT_RUNNING,
                    message="Provider 状态检测失败；未将其视为可用。",
                    error=f"{exc.__class__.__name__}: {exc}",
                )
        return result

    status_all = detect_all

    def capability_matrix(self) -> dict[str, dict[str, Capability]]:
        result = {}
        for name, provider in self._providers.items():
            try:
                result[name] = provider.capabilities()
            except Exception as exc:
                result[name] = {
                    "detect": Capability(
                        CapabilityState.NOT_RUNNING,
                        "能力检测失败，不能声明支持。",
                        f"{exc.__class__.__name__}: {exc}",
                    )
                }
        return result

    def close(self) -> None:
        for provider in self._providers.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        if self._owned_supervisor is not None:
            self._owned_supervisor.close()
            self._owned_supervisor = None


def create_default_registry(
    *, supervisor: ProcessSupervisor | None = None,
    ollama_url: str = "http://127.0.0.1:11434",
) -> ProviderRegistry:
    """创建四个内置 Provider；仅检测，不安装也不启动外部服务。"""
    owned_supervisor = supervisor or ProcessSupervisor()
    registry = ProviderRegistry([
        OllamaProvider(ollama_url),
        LlamaCppProvider(supervisor=owned_supervisor),
        LMStudioProvider(supervisor=owned_supervisor),
        VLLMProvider(supervisor=owned_supervisor),
    ])
    if supervisor is None:
        # 默认注册表拥有共享监管器，避免各 Provider 重复回收同一进程。
        registry._owned_supervisor = owned_supervisor
    return registry


default_registry = create_default_registry
