"""Agent 层可分类处理的异常。"""
from __future__ import annotations

from ..core.errors import ModelManagerError


class AgentError(ModelManagerError):
    """Agent 核心异常基类。"""


class ModelUnavailableError(AgentError):
    """指定模型不存在于本机 Ollama，不能伪造回复。"""


class SceneNotReadyError(AgentError):
    """场景尚未满足模型、装入或软件条件。"""


class WorkspaceSecurityError(AgentError):
    """工作区路径、文件类型或内容违反安全约束。"""


class CommandSecurityError(AgentError):
    """命令未确认、不可执行或不在白名单内。"""
