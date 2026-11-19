"""内置场景及自定义场景持久化；导入配置始终只是数据。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..core.errors import BackendNotInstalledError
from .models import EntryPoint, SceneDefinition, SceneKind, SoftwareRequirement


_CHAT_PROMPT = "你是本地模型管家内置的中文 Agent。只根据实际提供的信息回答，不声称执行过未发生的操作。"
_CODE_PROMPT = (
    "你是本地模型管家内置的 AI 编程 Agent。只能使用宿主明确提供的文件检索和读取结果，"
    "不得声称运行过 Python、shell 或测试。需要改动时，仅输出 unified diff 代码块，或输出 "
    '{"changes":[{"operation":"create|modify|delete","path":"相对路径","content":"完整新内容"}]} '
    "JSON（delete 不含 content）。所有改动都会由用户预览确认，不要声称已经写入。"
)
_MAINTENANCE_PROMPT = (
    "你是本地模型管家内置的 AI 工程维护 Agent。命令只作为可审查计划，默认供复制或在终端打开；"
    "不得声称命令已执行。说明程序、逐参数、工作目录、影响与风险。"
)


def builtin_scenes() -> list[SceneDefinition]:
    video_software = SoftwareRequirement(
        id="video-suite",
        name="已配置的视频生成/剪辑软件",
        purpose="仅生成启动配套软件的计划；Agent 对话仍留在本工具内。",
    )
    return [
        SceneDefinition(
            id="general-agent", kind=SceneKind.GENERAL, name="通用 Agent 对话",
            description="使用本机 Ollama 的内置流式对话。", system_prompt=_CHAT_PROMPT,
        ),
        SceneDefinition(
            id="video-understanding", kind=SceneKind.VIDEO_UNDERSTANDING,
            name="视频理解", description="围绕用户实际提供的帧、字幕或描述进行内置对话。",
            system_prompt=_CHAT_PROMPT + "未提供真实视频帧或识别结果时，不得声称看过视频。",
        ),
        SceneDefinition(
            id="video-generation", kind=SceneKind.VIDEO_GENERATION,
            name="视频生成与剪辑", description="内置策划对话，并按需生成配套软件启动计划。",
            system_prompt=_CHAT_PROMPT + "你只能生成方案与配套软件启动计划，不能伪造视频处理结果。",
            entry_point=EntryPoint.COMPANION_SOFTWARE, software=(video_software,),
        ),
        SceneDefinition(
            id="ai-coding", kind=SceneKind.CODING, name="AI 编程",
            description="内置检索、阅读、差异预览与确认写入。", system_prompt=_CODE_PROMPT,
            entry_point=EntryPoint.WORKSPACE,
        ),
        SceneDefinition(
            id="ai-maintenance", kind=SceneKind.MAINTENANCE, name="AI 工程维护",
            description="内置工程对话及可审查命令计划。", system_prompt=_MAINTENANCE_PROMPT,
            entry_point=EntryPoint.WORKSPACE,
        ),
    ]


class SceneRepository:
    """适配现有 scenes 表；无 store 时使用进程内仓储。"""

    def __init__(self, store: Any = None):
        self._store = store
        self._items: dict[str, SceneDefinition] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _decode(raw: str) -> SceneDefinition | None:
        try:
            data = json.loads(raw)
            scene = SceneDefinition.from_dict(data)
        except (TypeError, ValueError, KeyError):
            return None
        return scene if scene.id and scene.name else None

    def list(self) -> list[SceneDefinition]:
        if self._store is None:
            with self._lock:
                return [SceneDefinition.from_dict(item.to_dict()) for item in self._items.values()]
        rows = self._store.connection.execute(
            "SELECT config FROM scenes WHERE enabled=1 ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [scene for row in rows if (scene := self._decode(row["config"])) is not None]

    def get(self, scene_id: str) -> SceneDefinition | None:
        return next((item for item in self.list() if item.id == scene_id), None)

    def save(self, scene: SceneDefinition) -> SceneDefinition:
        if scene.kind != SceneKind.CUSTOM or not scene.id or not scene.name:
            raise ValueError("只能保存具有 id 和名称的自定义场景")
        if self._store is None:
            with self._lock:
                self._items[scene.id] = SceneDefinition.from_dict(scene.to_dict())
            return scene
        now = time.time()
        payload = json.dumps(scene.to_dict(), ensure_ascii=False, separators=(",", ":"))
        try:
            with self._store.transaction() as conn:
                rows = conn.execute("SELECT id, config FROM scenes").fetchall()
                for row in rows:
                    current = self._decode(row["config"])
                    if current is not None and current.id == scene.id:
                        conn.execute("DELETE FROM scenes WHERE id=?", (row["id"],))
                conn.execute(
                    """INSERT INTO scenes(name, description, config, enabled, created_at, updated_at)
                       VALUES (?, ?, ?, 1, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET description=excluded.description,
                         config=excluded.config, enabled=1, updated_at=excluded.updated_at""",
                    (scene.name, scene.description, payload, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"场景名称已存在：{scene.name}") from exc
        return scene

    def delete(self, scene_id: str) -> bool:
        if self._store is None:
            with self._lock:
                return self._items.pop(scene_id, None) is not None
        with self._store.transaction() as conn:
            rows = conn.execute("SELECT id, config FROM scenes").fetchall()
            ids = [
                row["id"] for row in rows
                if (scene := self._decode(row["config"])) is not None and scene.id == scene_id
            ]
            if not ids:
                return False
            conn.executemany("DELETE FROM scenes WHERE id=?", [(item,) for item in ids])
            return True


class SceneRegistry:
    def __init__(self, store: Any = None, repository: SceneRepository | None = None):
        self.repository = repository or SceneRepository(store)
        self._builtins = {item.id: item for item in builtin_scenes()}

    def list(self, include_custom: bool = True) -> list[SceneDefinition]:
        items = [SceneDefinition.from_dict(item.to_dict()) for item in self._builtins.values()]
        if include_custom:
            items.extend(self.repository.list())
        return items

    def get(self, scene_id: str) -> SceneDefinition:
        if scene_id in self._builtins:
            return SceneDefinition.from_dict(self._builtins[scene_id].to_dict())
        scene = self.repository.get(scene_id)
        if scene is None:
            raise KeyError(f"未知场景：{scene_id}")
        return scene

    def save_custom(self, scene: SceneDefinition) -> SceneDefinition:
        if scene.id in self._builtins:
            raise ValueError("不能覆盖内置场景")
        if any(item.name == scene.name and item.id != scene.id for item in self.list()):
            raise ValueError(f"场景名称已存在：{scene.name}")
        return self.repository.save(scene)

    def delete_custom(self, scene_id: str) -> bool:
        if scene_id in self._builtins:
            raise ValueError("不能删除内置场景")
        return self.repository.delete(scene_id)

    def import_custom(self, payload: str | dict[str, Any]) -> SceneDefinition:
        """仅反序列化白名单字段；任何 commands/config 内容都不会被自动执行。"""
        if isinstance(payload, str):
            if len(payload.encode("utf-8")) > 1024 * 1024:
                raise ValueError("场景配置超过 1 MiB 安全限制")
            data = json.loads(payload)
        elif isinstance(payload, dict):
            data = dict(payload)
        else:
            raise TypeError("场景配置必须是 JSON 字符串或对象")
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("自定义场景名称不能为空")
        scene_id = str(data.get("id", "")).strip() or f"custom-{uuid.uuid4().hex}"
        if scene_id in self._builtins:
            raise ValueError("自定义场景不能使用内置场景 id")
        try:
            entry = EntryPoint(data.get("entry_point", EntryPoint.CHAT.value))
        except ValueError as exc:
            raise ValueError("entry_point 仅支持 chat/workspace/companion_software") from exc
        requirements = []
        for index, item in enumerate(data.get("software", []), 1):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index} 项软件配置不是对象")
            requirement = SoftwareRequirement.from_dict(item)
            if not requirement.id or not requirement.name:
                raise ValueError("软件配置必须包含 id 和 name")
            requirements.append(requirement)
        known = {
            "id", "name", "description", "model", "system_prompt", "entry_point",
            "software", "config",
        }
        config = dict(data.get("config", {})) if isinstance(data.get("config"), dict) else {}
        imported = {key: value for key, value in data.items() if key not in known}
        if imported:
            config["imported_fields"] = imported
        scene = SceneDefinition(
            id=scene_id, kind=SceneKind.CUSTOM, name=name,
            description=str(data.get("description", "")), model=str(data.get("model", "")).strip(),
            system_prompt=str(data.get("system_prompt", "")) or _CHAT_PROMPT,
            entry_point=entry, software=tuple(requirements), config=config,
        )
        return self.save_custom(scene)

    def export_custom(self, scene_id: str) -> str:
        scene = self.get(scene_id)
        if scene.kind != SceneKind.CUSTOM:
            raise ValueError("只能导出自定义场景")
        return json.dumps(scene.to_dict(), ensure_ascii=False, indent=2)


def configured_executable(requirement: SoftwareRequirement, settings: Any = None) -> str:
    """读取显式配置；不在导入时启动软件，也不猜测下载成功。"""
    configured = requirement.executable
    if settings is not None:
        configured = str(settings.get(f"agent.software.{requirement.id}", configured) or configured)
    if not configured:
        return ""
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute() or not candidate.is_file():
        return ""
    try:
        return str(candidate.resolve(strict=True))
    except OSError as exc:
        raise BackendNotInstalledError(f"无法验证配套软件路径：{candidate}（{exc}）") from exc
