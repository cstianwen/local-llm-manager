"""本地模型后端公开 API。"""
from .base import (
    BaseProvider,
    Capability,
    CapabilityState,
    LoopbackHTTPClient,
    Provider,
    ProviderAPIError,
    ProviderStatus,
    check_cancelled,
    validate_loopback_url,
)
from .llamacpp import LlamaCppProvider
from .lmstudio import LMStudioProvider
from .ollama import DEFAULT_OLLAMA_URL, OllamaProvider
from .registry import ProviderRegistry, create_default_registry, default_registry
from .t4 import (
    DEFAULT_T4_MODEL,
    DEFAULT_T4_START_SCRIPT,
    DEFAULT_T4_STOP_SCRIPT,
    DEFAULT_T4_URL,
    T4Provider,
)
from .vllm import VLLMProvider

# 常见大小写别名，保持服务层调用简单。
LlamaCPPProvider = LlamaCppProvider
LMStudio = LMStudioProvider
VllmProvider = VLLMProvider

__all__ = [
    "BaseProvider", "Capability", "CapabilityState", "LoopbackHTTPClient",
    "Provider", "ProviderAPIError", "ProviderStatus", "ProviderRegistry",
    "OllamaProvider", "LlamaCppProvider", "LlamaCPPProvider", "LMStudioProvider",
    "LMStudio", "VLLMProvider", "VllmProvider", "T4Provider", "DEFAULT_OLLAMA_URL",
    "DEFAULT_T4_URL", "DEFAULT_T4_START_SCRIPT", "DEFAULT_T4_STOP_SCRIPT", "DEFAULT_T4_MODEL",
    "create_default_registry", "default_registry", "check_cancelled", "validate_loopback_url",
]
