"""本地模型管家内置 Agent 的无 Qt 生产核心。"""
from .commands import (
    CommandCenter,
    CommandConfirmation,
    CommandExecutionResult,
    CommandPlan,
)
from .engine import AgentEngine
from .errors import (
    AgentError,
    CommandSecurityError,
    ModelUnavailableError,
    SceneNotReadyError,
    WorkspaceSecurityError,
)
from .models import (
    AgentEvent,
    AgentEventType,
    AgentSession,
    ChangeOperation,
    ChatMessage,
    EntryPoint,
    SceneDefinition,
    SceneKind,
    ScenePhase,
    SceneState,
    SoftwareRequirement,
    SoftwareStatus,
)
from .protocol import ChangeSuggestion, SuggestionParseResult, parse_change_suggestions
from .scenes import SceneRegistry, SceneRepository, builtin_scenes
from .session import CancellationToken as AgentCancellationToken
from .session import SessionRepository
from .workspace import (
    ApplyResult,
    ChangeConfirmation,
    ChangeSet,
    CodingWorkspace,
    FileContent,
    FileMatch,
    PendingChange,
    SearchResult,
)


__all__ = [
    "AgentCancellationToken", "AgentEngine", "AgentError", "AgentEvent", "AgentEventType",
    "AgentSession", "ApplyResult", "ChangeConfirmation", "ChangeOperation", "ChangeSet",
    "ChangeSuggestion", "ChatMessage", "CodingWorkspace", "CommandCenter",
    "CommandConfirmation", "CommandExecutionResult", "CommandPlan", "CommandSecurityError",
    "EntryPoint", "FileContent", "FileMatch", "ModelUnavailableError", "PendingChange",
    "SceneDefinition", "SceneKind", "SceneNotReadyError", "ScenePhase", "SceneRegistry",
    "SceneRepository", "SceneState", "SearchResult", "SessionRepository",
    "SoftwareRequirement", "SoftwareStatus", "SuggestionParseResult", "WorkspaceSecurityError",
    "builtin_scenes", "parse_change_suggestions",
]
