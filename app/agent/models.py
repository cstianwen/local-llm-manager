"""Agent 领域的纯 Python 数据结构，供任意 UI 直接绑定。"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SceneKind(str, Enum):
    GENERAL = "general"
    VIDEO_UNDERSTANDING = "video_understanding"
    VIDEO_GENERATION = "video_generation"
    CODING = "coding"
    MAINTENANCE = "maintenance"
    CUSTOM = "custom"


class ScenePhase(str, Enum):
    IDLE = "idle"
    CHECKING = "checking"
    PROVIDER_OFFLINE = "provider_offline"
    NEEDS_MODEL = "needs_model"
    NEEDS_DOWNLOAD = "needs_download"
    NEEDS_LOAD = "needs_load"
    NEEDS_SOFTWARE = "needs_software"
    NEEDS_WORKSPACE = "needs_workspace"
    READY = "ready"
    STREAMING = "streaming"
    CANCELLED = "cancelled"
    ERROR = "error"


class EntryPoint(str, Enum):
    CHAT = "chat"
    WORKSPACE = "workspace"
    COMPANION_SOFTWARE = "companion_software"


class ChangeOperation(str, Enum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class AgentEventType(str, Enum):
    STATE = "state"
    USER_MESSAGE = "user_message"
    ASSISTANT_STARTED = "assistant_started"
    TOKEN = "token"
    ASSISTANT_COMPLETED = "assistant_completed"
    CANCELLED = "cancelled"
    ERROR = "error"
    DOWNLOAD_PROGRESS = "download_progress"
    CHANGE_PREVIEW = "change_preview"
    COMMAND_PLAN = "command_plan"
    PROCESS_OUTPUT = "process_output"


@dataclass(frozen=True)
class SoftwareRequirement:
    id: str
    name: str
    executable: str = ""
    download_url: str = ""
    purpose: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SoftwareRequirement":
        return cls(
            id=str(data.get("id", "")).strip(),
            name=str(data.get("name", "")).strip(),
            executable=str(data.get("executable", "")).strip(),
            download_url=str(data.get("download_url", "")).strip(),
            purpose=str(data.get("purpose", "")).strip(),
        )


@dataclass
class SoftwareStatus:
    id: str
    name: str
    installed: bool = False
    executable: str = ""
    download_url: str = ""
    purpose: str = ""
    message: str = ""


@dataclass
class SceneDefinition:
    id: str
    kind: SceneKind
    name: str
    description: str = ""
    model: str = ""
    system_prompt: str = ""
    entry_point: EntryPoint = EntryPoint.CHAT
    software: tuple[SoftwareRequirement, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "description": self.description,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "entry_point": self.entry_point.value,
            "software": [asdict(item) for item in self.software],
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SceneDefinition":
        software = tuple(
            SoftwareRequirement.from_dict(item)
            for item in data.get("software", [])
            if isinstance(item, dict)
        )
        config = data.get("config", {})
        return cls(
            id=str(data.get("id", "")).strip(),
            kind=SceneKind(data.get("kind", SceneKind.CUSTOM.value)),
            name=str(data.get("name", "")).strip(),
            description=str(data.get("description", "")),
            model=str(data.get("model", "")).strip(),
            system_prompt=str(data.get("system_prompt", "")),
            entry_point=EntryPoint(data.get("entry_point", EntryPoint.CHAT.value)),
            software=software,
            config=dict(config) if isinstance(config, dict) else {},
        )


@dataclass
class SceneState:
    scene_id: str
    phase: ScenePhase = ScenePhase.IDLE
    model: str = ""
    provider: dict[str, Any] = field(default_factory=dict)
    software: list[SoftwareStatus] = field(default_factory=list)
    workspace_root: str = ""
    message: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.phase == ScenePhase.READY

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChatMessage:
    session_id: str
    role: str
    content: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    status: str = "completed"
    model: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatMessage":
        names = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in names})


@dataclass
class AgentSession:
    scene_id: str
    scene_kind: SceneKind
    model: str = ""
    title: str = ""
    system_prompt: str = ""
    workspace_root: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: str = "idle"
    messages: list[ChatMessage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scene_id": self.scene_id,
            "scene_kind": self.scene_kind.value,
            "model": self.model,
            "title": self.title,
            "system_prompt": self.system_prompt,
            "workspace_root": self.workspace_root,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "messages": [message.to_dict() for message in self.messages],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSession":
        return cls(
            id=str(data.get("id", "")),
            scene_id=str(data.get("scene_id", "")),
            scene_kind=SceneKind(data.get("scene_kind", SceneKind.GENERAL.value)),
            model=str(data.get("model", "")),
            title=str(data.get("title", "")),
            system_prompt=str(data.get("system_prompt", "")),
            workspace_root=str(data.get("workspace_root", "")),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            status=str(data.get("status", "idle")),
            messages=[
                ChatMessage.from_dict(item)
                for item in data.get("messages", [])
                if isinstance(item, dict)
            ],
            metadata=dict(data.get("metadata", {})) if isinstance(data.get("metadata"), dict) else {},
        )


@dataclass(frozen=True)
class AgentEvent:
    type: AgentEventType
    session_id: str = ""
    scene_id: str = ""
    state: str = ""
    message_id: str = ""
    delta: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["type"] = self.type.value
        return result
