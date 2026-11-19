"""无 Qt 的内置 Agent 引擎：真实 Ollama 流、会话、场景及安全工具入口。"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from ..core.errors import BackendOfflineError, OperationCancelledError
from ..core.models import DownloadProgress
from ..core.settings import Settings
from ..providers.base import Provider, ProviderAPIError
from ..providers.ollama import OllamaProvider
from .commands import CommandCenter, CommandConfirmation, CommandExecutionResult, CommandPlan
from .errors import AgentError, ModelUnavailableError, SceneNotReadyError
from .models import (
    AgentEvent,
    AgentEventType,
    AgentSession,
    ChatMessage,
    EntryPoint,
    SceneDefinition,
    SceneKind,
    ScenePhase,
    SceneState,
    SoftwareStatus,
)
from .protocol import SuggestionParseResult
from .scenes import SceneRegistry, configured_executable
from .session import CancellationToken, SessionRepository
from .workspace import (
    ApplyResult,
    ChangeConfirmation,
    ChangeSet,
    CodingWorkspace,
    FileContent,
    SearchResult,
)


_LOG = logging.getLogger(__name__)
EventCallback = Callable[[AgentEvent], None]


class AgentEngine:
    """同步/生成器 API 可由 UI 放入任意工作线程；不在导入或初始化时联网。"""

    def __init__(
        self,
        provider: OllamaProvider,
        store: Any = None,
        *,
        workspace: CodingWorkspace | None = None,
        command_center: CommandCenter | None = None,
        scene_registry: SceneRegistry | None = None,
        session_repository: SessionRepository | None = None,
        on_event: EventCallback | None = None,
        fallback_provider: Provider | None = None,
    ):
        self.provider = provider
        self.fallback_provider = fallback_provider
        self.store = store
        self.settings = Settings(store) if store is not None else None
        self.workspace = workspace or CodingWorkspace()
        self.commands = command_center or CommandCenter()
        self.scenes = scene_registry or SceneRegistry(store)
        self.sessions = session_repository or SessionRepository(store)
        self._callbacks: list[EventCallback] = []
        self._states: dict[str, SceneState] = {}
        self._active: dict[str, CancellationToken] = {}
        self._lock = threading.RLock()
        if on_event is not None:
            self._callbacks.append(on_event)

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        with self._lock:
            self._callbacks.append(callback)
        def unsubscribe() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)
        return unsubscribe

    def _emit(self, event: AgentEvent) -> AgentEvent:
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                _LOG.exception("Agent 事件回调失败")
        return event

    def _set_state(
        self,
        scene_id: str,
        phase: ScenePhase,
        *,
        model: str = "",
        message: str = "",
        error: str = "",
        provider: dict[str, Any] | None = None,
        software: list[SoftwareStatus] | None = None,
        workspace_root: str = "",
        data: dict[str, Any] | None = None,
    ) -> SceneState:
        state = SceneState(
            scene_id=scene_id, phase=phase, model=model, message=message, error=error,
            provider=provider or {}, software=software or [], workspace_root=workspace_root,
            data=data or {},
        )
        self._states[scene_id] = state
        self._emit(AgentEvent(
            AgentEventType.STATE, scene_id=scene_id, state=phase.value,
            error=error, data={"scene_state": state.to_dict()},
        ))
        return state

    def get_scene_state(self, scene_id: str) -> SceneState:
        return self._states.get(scene_id, SceneState(scene_id=scene_id))

    def prepare_scene(
        self,
        scene_id: str,
        *,
        model: str | None = None,
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> SceneState:
        """检查模型/软件，返回需要下载、装入、选工作区或就绪的真实状态。"""
        scene = self.scenes.get(scene_id)
        selected = (model if model is not None else scene.model).strip()
        self._set_state(scene_id, ScenePhase.CHECKING, model=selected, message="正在检查本机 Ollama")
        try:
            try:
                status = self.provider.detect()
            except Exception as exc:
                if self.fallback_provider is not None:
                    return self._prepare_t4_fallback(
                        scene, selected, workspace_root, f"Ollama 状态检测不可用：{exc}"
                    )
                raise
            provider_data = status.to_dict()
            if not status.running:
                if self.fallback_provider is not None:
                    return self._prepare_t4_fallback(
                        scene, selected, workspace_root,
                        status.error or status.message or "Ollama 服务未运行",
                        ollama_status=provider_data,
                    )
                return self._set_state(
                    scene_id, ScenePhase.PROVIDER_OFFLINE, model=selected,
                    message=status.message, error=status.error or status.message,
                    provider=provider_data,
                )
            try:
                models = self.provider.list_models()
            except Exception as exc:
                if self.fallback_provider is not None:
                    return self._prepare_t4_fallback(
                        scene, selected, workspace_root, f"Ollama 模型列表不可用：{exc}",
                        ollama_status=provider_data,
                    )
                raise
            available = [item.model_ref or item.file_name for item in models]
            if not selected:
                return self._set_state(
                    scene_id, ScenePhase.NEEDS_MODEL, message="请选择一个已存在于本机 Ollama 的模型",
                    provider=provider_data, data={"available_models": available},
                )
            if selected not in available:
                return self._set_state(
                    scene_id, ScenePhase.NEEDS_DOWNLOAD, model=selected,
                    message=f"模型 {selected} 未登记在本机 Ollama；需要用户明确下载，当前未下载。",
                    provider=provider_data,
                    data={"available_models": available, "download_required": True},
                )
            running = self.provider.running_models()
            loaded = [item.name for item in running]
            if selected not in loaded:
                return self._set_state(
                    scene_id, ScenePhase.NEEDS_LOAD, model=selected,
                    message=f"模型 {selected} 已安装但未装入；请明确执行装入。",
                    provider=provider_data, data={"load_required": True},
                )
            software: list[SoftwareStatus] = []
            for requirement in scene.software:
                executable = configured_executable(requirement, self.settings)
                installed = bool(executable)
                software.append(SoftwareStatus(
                    id=requirement.id, name=requirement.name, installed=installed,
                    executable=executable, download_url=requirement.download_url,
                    purpose=requirement.purpose,
                    message="已验证配置的本地程序。" if installed else "未配置或找不到本地配套软件；未自动下载或启动。",
                ))
            if any(not item.installed for item in software):
                return self._set_state(
                    scene_id, ScenePhase.NEEDS_SOFTWARE, model=selected,
                    message="配套软件不可用；内置对话仍可使用，启动软件前需先配置真实程序。",
                    provider=provider_data, software=software,
                    data={"software_required": True, "chat_available": True},
                )
            if scene.entry_point == EntryPoint.WORKSPACE:
                if workspace_root is not None:
                    selected_root = self.workspace.select_root(workspace_root)
                else:
                    selected_root = self.workspace.root
                if not selected_root:
                    return self._set_state(
                        scene_id, ScenePhase.NEEDS_WORKSPACE, model=selected,
                        message="AI 编程/工程维护必须显式选择具体工作区根目录。",
                        provider=provider_data, software=software,
                    )
            else:
                selected_root = self.workspace.root if workspace_root is not None else ""
            return self._set_state(
                scene_id, ScenePhase.READY, model=selected,
                message="模型已装入，场景可以进入。" if scene.entry_point != EntryPoint.COMPANION_SOFTWARE
                else "模型和配套软件已就绪；可请求生成启动计划，当前尚未启动。",
                provider=provider_data, software=software, workspace_root=selected_root,
                data={
                    "entry_point": scene.entry_point.value,
                    "requires_software_start": scene.entry_point == EntryPoint.COMPANION_SOFTWARE,
                },
            )
        except Exception as exc:
            return self._set_state(
                scene_id, ScenePhase.ERROR, model=selected,
                message="场景检查失败，未进入工作窗或启动软件。", error=str(exc),
            )

    def _prepare_t4_fallback(
        self,
        scene: SceneDefinition,
        requested_model: str,
        workspace_root: str | os.PathLike[str] | None,
        fallback_reason: str,
        *,
        ollama_status: dict[str, Any] | None = None,
    ) -> SceneState:
        """只检测显式提供的 T4；任何失败都不得转变为自动启动。"""
        fallback = self.fallback_provider
        if fallback is None:
            raise BackendOfflineError(fallback_reason)
        reason = str(fallback_reason).strip() or "Ollama 不可用"
        note = f"active_provider='t4'；fallback_reason={reason}"
        provider_data: dict[str, Any] = {
            "provider": "t4", "active_provider": "t4", "fallback_reason": reason,
        }
        data: dict[str, Any] = {
            "active_provider": "t4",
            "fallback": True,
            "fallback_reason": reason,
            "requested_model": requested_model,
            "ollama_status": ollama_status or {},
        }
        try:
            status = fallback.detect()
            provider_data = {**status.to_dict(), "active_provider": "t4", "fallback_reason": reason}
        except Exception as exc:
            message = (
                f"Ollama 不可用，T4 状态检测也失败：{exc}。请手动启动 T4 后重试；"
                f"本工具不会自动执行脚本。{note}"
            )
            return self._set_state(
                scene.id, ScenePhase.PROVIDER_OFFLINE, model=requested_model,
                message=message, error=str(exc), provider=provider_data,
                data={
                    **data, "action": "NEEDS_START", "code": "NEEDS_START",
                    "needs_start": True, "manual_start_required": True,
                    "start_script": getattr(fallback, "start_script", ""),
                },
            )
        if not status.running:
            manual = (
                f"请由用户手动调用 T4Provider.start_service 或执行 "
                f"{getattr(fallback, 'start_script', '配置的 T4 启动脚本')}；本工具不会自动启动。"
            )
            message = f"Ollama 不可用，T4 未就绪。{manual}{note}"
            return self._set_state(
                scene.id, ScenePhase.PROVIDER_OFFLINE, model=requested_model,
                message=message, error=status.error or status.message,
                provider=provider_data,
                data={
                    **data, "action": "NEEDS_START", "code": "NEEDS_START",
                    "needs_start": True, "manual_start_required": True,
                    "start_script": getattr(fallback, "start_script", ""),
                    "t4_status": status.to_dict(),
                },
            )
        try:
            models = fallback.list_models()
            if not models:
                raise ProviderAPIError("T4 健康服务没有报告固定模型", provider="t4")
            active_model = models[0].model_ref or models[0].file_name
            if not active_model:
                raise ProviderAPIError("T4 固定模型名称为空", provider="t4")
        except Exception as exc:
            return self._set_state(
                scene.id, ScenePhase.ERROR, model=requested_model,
                message=f"T4 已健康，但固定模型映射失败；{note}",
                error=str(exc), provider=provider_data, data=data,
            )
        data.update({
            "active_model": active_model,
            "available_models": [item.model_ref or item.file_name for item in models],
        })

        software: list[SoftwareStatus] = []
        for requirement in scene.software:
            executable = configured_executable(requirement, self.settings)
            installed = bool(executable)
            software.append(SoftwareStatus(
                id=requirement.id, name=requirement.name, installed=installed,
                executable=executable, download_url=requirement.download_url,
                purpose=requirement.purpose,
                message="已验证配置的本地程序。" if installed else "未配置或找不到本地配套软件；未自动下载或启动。",
            ))
        if any(not item.installed for item in software):
            return self._set_state(
                scene.id, ScenePhase.NEEDS_SOFTWARE, model=active_model,
                message=f"Ollama 不可用，已回退 T4；配套软件不可用，内置对话仍可使用。{note}",
                provider=provider_data, software=software,
                data={**data, "software_required": True, "chat_available": True},
            )
        if scene.entry_point == EntryPoint.WORKSPACE:
            if workspace_root is not None:
                selected_root = self.workspace.select_root(workspace_root)
            else:
                selected_root = self.workspace.root
            if not selected_root:
                return self._set_state(
                    scene.id, ScenePhase.NEEDS_WORKSPACE, model=active_model,
                    message=f"已回退 T4；AI 编程/工程维护仍需显式选择工作区。{note}",
                    provider=provider_data, software=software, data=data,
                )
        else:
            selected_root = self.workspace.root if workspace_root is not None else ""
        return self._set_state(
            scene.id, ScenePhase.READY, model=active_model,
            message=f"Ollama 不可用，已使用健康的 T4 固定服务。{note}",
            provider=provider_data, software=software, workspace_root=selected_root,
            data={
                **data,
                "entry_point": scene.entry_point.value,
                "requires_software_start": scene.entry_point == EntryPoint.COMPANION_SOFTWARE,
            },
        )

    def prepare_session(self, session_id: str) -> SceneState:
        session = self.sessions.require(session_id)
        return self.prepare_scene(
            session.scene_id, model=session.model,
            workspace_root=session.workspace_root or None,
        )

    def load_scene_model(self, scene_id: str, model: str | None = None, **options: Any) -> SceneState:
        scene = self.scenes.get(scene_id)
        selected = (model if model is not None else scene.model).strip()
        if not selected:
            return self._set_state(scene_id, ScenePhase.NEEDS_MODEL, message="装入前必须选择模型")
        try:
            result = self.provider.load_model(selected, **options)
            if not result.success:
                return self._set_state(
                    scene_id, ScenePhase.ERROR, model=selected,
                    message=result.message or "Ollama 未确认装入", error=result.error,
                    data={"load_result": result.to_dict()},
                )
        except Exception as exc:
            return self._set_state(
                scene_id, ScenePhase.ERROR, model=selected,
                message="模型装入失败", error=str(exc),
            )
        state = self.prepare_scene(scene_id, model=selected)
        state.data["load_result"] = result.to_dict()
        return state

    def pull_model(
        self,
        scene_id: str,
        model: str,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> Iterator[DownloadProgress]:
        """仅由用户显式调用；可能由本机 Ollama 访问模型仓库，绝不自动执行。"""
        try:
            for progress in self.provider.pull_model(model, cancel_token=cancel_token):
                self._emit(AgentEvent(
                    AgentEventType.DOWNLOAD_PROGRESS, scene_id=scene_id,
                    data={"progress": progress.to_dict()},
                ))
                yield progress
            self.prepare_scene(scene_id, model=model)
        except Exception as exc:
            self._set_state(scene_id, ScenePhase.ERROR, model=model, message="模型下载未完成", error=str(exc))
            raise

    def create_software_start_plans(self, scene_id: str, *, model: str | None = None) -> list[CommandPlan]:
        scene = self.scenes.get(scene_id)
        if scene.entry_point != EntryPoint.COMPANION_SOFTWARE:
            raise SceneNotReadyError("该场景不需要启动配套软件")
        state = self.prepare_scene(scene_id, model=model)
        if state.phase != ScenePhase.READY:
            raise SceneNotReadyError(state.message or state.error or "场景尚未就绪")
        plans = []
        for software in state.software:
            plan = self.commands.create_companion_plan(
                software.executable, purpose=software.purpose or scene.name,
            )
            plans.append(plan)
            self._emit(AgentEvent(
                AgentEventType.COMMAND_PLAN, scene_id=scene_id, data={"plan": plan.to_dict()},
            ))
        return plans

    @staticmethod
    def _scene_id(scene: str | SceneKind) -> str:
        if isinstance(scene, SceneKind):
            return {
                SceneKind.GENERAL: "general-agent",
                SceneKind.VIDEO_UNDERSTANDING: "video-understanding",
                SceneKind.VIDEO_GENERATION: "video-generation",
                SceneKind.CODING: "ai-coding",
                SceneKind.MAINTENANCE: "ai-maintenance",
            }.get(scene, "")
        return scene

    def create_session(
        self,
        scene: str | SceneKind,
        *,
        model: str = "",
        title: str = "",
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> AgentSession:
        definition = self.scenes.get(self._scene_id(scene))
        selected_root = self.workspace.select_root(workspace_root) if workspace_root is not None else ""
        session = AgentSession(
            scene_id=definition.id, scene_kind=definition.kind,
            model=model.strip() or definition.model, title=title.strip() or definition.name,
            system_prompt=definition.system_prompt, workspace_root=selected_root,
        )
        self.sessions.save(session)
        return session

    def get_session(self, session_id: str) -> AgentSession:
        return self.sessions.require(session_id)

    def list_sessions(self, limit: int = 100) -> list[AgentSession]:
        return self.sessions.list(limit)

    def delete_session(self, session_id: str) -> bool:
        if self.cancel(session_id):
            raise AgentError("会话正在取消，请等待流结束后再删除")
        return self.sessions.delete(session_id)

    def select_workspace(
        self, root: str | os.PathLike[str], *, session_id: str | None = None
    ) -> str:
        selected = self.workspace.select_root(root)
        if session_id is not None:
            session = self.sessions.require(session_id)
            session.workspace_root = selected
            session.updated_at = time.time()
            self.sessions.save(session)
        return selected

    def _session_workspace(self, session: AgentSession) -> str:
        if not session.workspace_root:
            raise SceneNotReadyError("请先显式选择编程工作区根目录")
        if self.workspace.root != session.workspace_root:
            self.workspace.select_root(session.workspace_root)
        return session.workspace_root

    def search_workspace(
        self, session_id: str, query: str = "", *, content: bool = False,
        pattern: str = "*", max_results: int | None = None,
    ) -> SearchResult:
        session = self.sessions.require(session_id)
        self._session_workspace(session)
        return self.workspace.search_files(
            query, content=content, pattern=pattern, max_results=max_results,
        )

    def read_workspace_file(
        self, session_id: str, path: str, *, max_bytes: int | None = None
    ) -> FileContent:
        session = self.sessions.require(session_id)
        self._session_workspace(session)
        return self.workspace.read_text(path, max_bytes=max_bytes)

    def preview_model_response(
        self, session_id: str, text: str | None = None, *, message_id: str | None = None
    ) -> tuple[ChangeSet, SuggestionParseResult]:
        session = self.sessions.require(session_id)
        self._session_workspace(session)
        if text is None:
            messages = [
                item for item in session.messages
                if item.role == "assistant" and (message_id is None or item.id == message_id)
            ]
            if not messages:
                raise KeyError("找不到可解析的助手消息")
            text = messages[-1].content
        change_set, parsed = self.workspace.preview_model_response(text)
        self._emit(AgentEvent(
            AgentEventType.CHANGE_PREVIEW, session_id=session_id, scene_id=session.scene_id,
            data={"change_set": change_set.to_dict(), "parse_issues": parsed.issues},
        ))
        return change_set, parsed

    def request_change_confirmation(
        self, session_id: str, change_id: str
    ) -> ChangeConfirmation:
        session = self.sessions.require(session_id)
        root = self._session_workspace(session)
        return self.workspace.request_confirmation(change_id, root)

    def apply_change(
        self, session_id: str, change_id: str, confirmation: ChangeConfirmation
    ) -> ApplyResult:
        session = self.sessions.require(session_id)
        root = self._session_workspace(session)
        return self.workspace.apply_change(change_id, confirmation, root)

    @staticmethod
    def _history(session: AgentSession, assistant_id: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if session.system_prompt:
            messages.append({"role": "system", "content": session.system_prompt})
        for item in session.messages:
            if item.id == assistant_id or not item.content:
                continue
            if item.role == "assistant" and item.status != "completed":
                continue
            if item.role in {"user", "assistant"}:
                messages.append({"role": item.role, "content": item.content})
        return messages

    def _check_chat_ready(self, session: AgentSession) -> SceneState:
        state = self.prepare_scene(
            session.scene_id, model=session.model,
            workspace_root=session.workspace_root or None,
        )
        if state.phase == ScenePhase.PROVIDER_OFFLINE:
            if state.data.get("active_provider") == "t4":
                # 回退离线必须保留 NEEDS_START 对应的中文手动启动说明，而非只暴露连接错误。
                raise BackendOfflineError(state.message or state.error)
            raise BackendOfflineError(state.error or state.message)
        if state.phase in {ScenePhase.NEEDS_MODEL, ScenePhase.NEEDS_DOWNLOAD}:
            raise ModelUnavailableError(state.message)
        if state.phase in {ScenePhase.NEEDS_LOAD, ScenePhase.NEEDS_WORKSPACE, ScenePhase.ERROR}:
            raise SceneNotReadyError(state.error or state.message)
        # NEEDS_SOFTWARE 只禁止配套软件启动，不禁止本工具内置文本对话。
        return state

    def stream_reply(
        self,
        session_id: str,
        prompt: str,
        *,
        options: dict[str, Any] | None = None,
        keep_alive: Any = None,
        use_generate: bool = False,
    ) -> Iterator[AgentEvent]:
        """产生并广播事件；错误/取消也作为事件返回，不伪造助手成功。"""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("对话输入不能为空")
        session = self.sessions.require(session_id)
        with self._lock:
            if session_id in self._active:
                raise AgentError("该会话已有正在进行的回复")
            token = CancellationToken()
            self._active[session_id] = token
        user = ChatMessage(session_id=session_id, role="user", content=prompt)
        assistant = ChatMessage(
            session_id=session_id, role="assistant", status="streaming", model=session.model,
        )
        session.messages.extend([user, assistant])
        session.status = "streaming"
        session.updated_at = time.time()
        self.sessions.save(session)
        done_event: dict[str, Any] | None = None
        readiness: SceneState | None = None
        try:
            yield self._emit(AgentEvent(
                AgentEventType.USER_MESSAGE, session_id=session_id, scene_id=session.scene_id,
                message_id=user.id, data={"message": user.to_dict()},
            ))
            yield self._emit(AgentEvent(
                AgentEventType.ASSISTANT_STARTED, session_id=session_id, scene_id=session.scene_id,
                state=ScenePhase.STREAMING.value, message_id=assistant.id,
                data={"message": assistant.to_dict()},
            ))
            readiness = self._check_chat_ready(session)
            active_provider = str(readiness.data.get("active_provider") or "ollama")
            active_backend = self.fallback_provider if active_provider == "t4" else self.provider
            if active_backend is None:
                raise BackendOfflineError("T4 回退 Provider 未配置")
            active_model = readiness.model if active_provider == "t4" else session.model
            if active_provider == "t4":
                assistant.model = active_model
                assistant.metadata["active_provider"] = "t4"
                assistant.metadata["fallback_reason"] = readiness.data.get("fallback_reason", "")
            self._set_state(
                session.scene_id, ScenePhase.STREAMING, model=active_model,
                message=(
                    "正在接收本机 Ollama 流式回复" if active_provider == "ollama" else
                    f"正在接收 T4 回退回复；active_provider='t4'；"
                    f"fallback_reason={readiness.data.get('fallback_reason', '')}"
                ),
                provider=readiness.provider, software=readiness.software,
                workspace_root=session.workspace_root, data=readiness.data,
            )
            history = self._history(session, assistant.id)
            transcript = "\n\n".join(f"{item['role']}: {item['content']}" for item in history)
            while True:
                try:
                    if use_generate:
                        stream = active_backend.generate(
                            active_model, transcript, options=options, keep_alive=keep_alive,
                            cancel_token=token,
                        )
                    else:
                        stream = active_backend.chat(
                            active_model, history, options=options, keep_alive=keep_alive,
                            cancel_token=token,
                        )
                    for event in stream:
                        token.raise_if_cancelled()
                        if use_generate:
                            delta = event.get("response", "")
                        else:
                            message = event.get("message", {})
                            delta = message.get("content", "") if isinstance(message, dict) else ""
                        if not isinstance(delta, str):
                            raise ProviderAPIError(
                                f"{'Ollama' if active_provider == 'ollama' else 'T4'} 流式 content/response 不是字符串",
                                provider=active_provider,
                            )
                        if delta:
                            assistant.content += delta
                            yield self._emit(AgentEvent(
                                AgentEventType.TOKEN, session_id=session_id, scene_id=session.scene_id,
                                message_id=assistant.id, delta=delta,
                            ))
                        if event.get("done") is True:
                            done_event = event
                    token.raise_if_cancelled()
                    if done_event is None:
                        raise ProviderAPIError(
                            f"{'Ollama' if active_provider == 'ollama' else 'T4'} 流在未收到 done=true 前结束，不能视为完整回复",
                            provider=active_provider,
                        )
                    break
                except (BackendOfflineError, ProviderAPIError) as exc:
                    # 已产生任何文本后不重试，避免把两个后端的答案拼接成伪成功回复。
                    if (
                        active_provider != "ollama" or assistant.content
                        or self.fallback_provider is None
                    ):
                        raise
                    reason = f"Ollama 推理不可用：{exc}"
                    fallback_state = self._prepare_t4_fallback(
                        self.scenes.get(session.scene_id), session.model,
                        session.workspace_root or None, reason,
                        ollama_status=readiness.provider,
                    )
                    readiness = fallback_state
                    if fallback_state.phase not in {ScenePhase.READY, ScenePhase.NEEDS_SOFTWARE}:
                        raise BackendOfflineError(
                            fallback_state.error or fallback_state.message
                        ) from exc
                    active_provider = "t4"
                    active_backend = self.fallback_provider
                    active_model = fallback_state.model
                    done_event = None
                    assistant.model = active_model
                    assistant.metadata["active_provider"] = "t4"
                    assistant.metadata["fallback_reason"] = reason
                    self._set_state(
                        session.scene_id, ScenePhase.STREAMING, model=active_model,
                        message=(
                            f"Ollama 流不可用，正在接收 T4 回退回复；active_provider='t4'；"
                            f"fallback_reason={reason}"
                        ),
                        provider=readiness.provider, software=readiness.software,
                        workspace_root=session.workspace_root, data=readiness.data,
                    )
            assistant.status = "completed"
            assistant.metadata["done"] = done_event
            session.status = "idle"
            session.updated_at = time.time()
            self.sessions.save(session)
            final_phase = (
                ScenePhase.NEEDS_SOFTWARE
                if readiness is not None and readiness.phase == ScenePhase.NEEDS_SOFTWARE
                else ScenePhase.READY
            )
            if active_provider == "t4":
                final_message = (
                    f"T4 回退回复已完成；active_provider='t4'；"
                    f"fallback_reason={readiness.data.get('fallback_reason', '')}"
                )
                if final_phase == ScenePhase.NEEDS_SOFTWARE:
                    final_message += "；配套软件仍未就绪"
            else:
                final_message = (
                    "本机回复已完成；配套软件仍未就绪。" if final_phase == ScenePhase.NEEDS_SOFTWARE
                    else "本机回复已完成"
                )
            self._set_state(
                session.scene_id, final_phase, model=active_model,
                message=final_message,
                provider=readiness.provider if readiness else {},
                software=readiness.software if readiness else [],
                workspace_root=session.workspace_root,
                data=readiness.data if readiness else {},
            )
            yield self._emit(AgentEvent(
                AgentEventType.ASSISTANT_COMPLETED, session_id=session_id, scene_id=session.scene_id,
                message_id=assistant.id, data={"message": assistant.to_dict()},
            ))
        except OperationCancelledError:
            assistant.status = "cancelled"
            session.status = "cancelled"
            session.updated_at = time.time()
            self.sessions.save(session)
            self._set_state(
                session.scene_id, ScenePhase.CANCELLED, model=session.model,
                message="回复已取消；仅保留真实收到的部分内容。", workspace_root=session.workspace_root,
            )
            yield self._emit(AgentEvent(
                AgentEventType.CANCELLED, session_id=session_id, scene_id=session.scene_id,
                state=ScenePhase.CANCELLED.value, message_id=assistant.id,
                data={"message": assistant.to_dict()},
            ))
        except Exception as exc:
            assistant.status = "error"
            assistant.error = str(exc)
            session.status = "error"
            session.updated_at = time.time()
            self.sessions.save(session)
            if readiness is not None:
                is_t4 = readiness.data.get("active_provider") == "t4"
                self._set_state(
                    session.scene_id, ScenePhase.ERROR,
                    model=readiness.model if is_t4 else session.model,
                    message=(
                        f"T4 回退回复流失败；active_provider='t4'；"
                        f"fallback_reason={readiness.data.get('fallback_reason', '')}"
                        if is_t4 else "本机回复流失败，未标记为成功。"
                    ),
                    error=str(exc), provider=readiness.provider, software=readiness.software,
                    workspace_root=session.workspace_root,
                    data=readiness.data if is_t4 else {},
                )
            yield self._emit(AgentEvent(
                AgentEventType.ERROR, session_id=session_id, scene_id=session.scene_id,
                message_id=assistant.id, error=str(exc), data={"message": assistant.to_dict()},
            ))
        finally:
            with self._lock:
                current = self._active.get(session_id)
                if current is token:
                    self._active.pop(session_id, None)
            if assistant.status == "streaming":
                # 调用方提前关闭生成器时保存真实部分内容，不留“进行中”假状态。
                assistant.status = "cancelled"
                session.status = "cancelled"
                session.updated_at = time.time()
                self.sessions.save(session)
                self._set_state(
                    session.scene_id, ScenePhase.CANCELLED, model=session.model,
                    message="回复流已由调用方提前关闭；仅保存真实部分内容。",
                    workspace_root=session.workspace_root,
                )
                self._emit(AgentEvent(
                    AgentEventType.CANCELLED, session_id=session_id, scene_id=session.scene_id,
                    state=ScenePhase.CANCELLED.value, message_id=assistant.id,
                    data={"message": assistant.to_dict()},
                ))

    def reply(self, session_id: str, prompt: str, **kwargs: Any) -> ChatMessage:
        assistant_id = ""
        for event in self.stream_reply(session_id, prompt, **kwargs):
            if event.type == AgentEventType.ASSISTANT_STARTED:
                assistant_id = event.message_id
        session = self.sessions.require(session_id)
        assistant = next(item for item in session.messages if item.id == assistant_id)
        if assistant.status == "error":
            raise AgentError(assistant.error)
        if assistant.status == "cancelled":
            raise OperationCancelledError("Agent 回复已取消；部分内容已保存")
        return assistant

    def cancel(self, session_id: str) -> bool:
        with self._lock:
            token = self._active.get(session_id)
        if token is None:
            return False
        token.cancel()
        session = self.sessions.get(session_id)
        if session is not None:
            session.status = "cancelling"
            session.updated_at = time.time()
            self.sessions.save(session)
        return True

    def create_command_plan(self, *args: Any, **kwargs: Any) -> CommandPlan:
        plan = self.commands.create_plan(*args, **kwargs)
        self._emit(AgentEvent(AgentEventType.COMMAND_PLAN, data={"plan": plan.to_dict()}))
        return plan

    def execute_command_plan(
        self,
        plan_id: str,
        confirmation: CommandConfirmation,
        *,
        session_id: str = "",
    ) -> CommandExecutionResult:
        def on_log(process: Any, line: str) -> None:
            self._emit(AgentEvent(
                AgentEventType.PROCESS_OUTPUT, session_id=session_id,
                data={"process_id": process.id, "pid": process.pid, "line": line},
            ))
        return self.commands.execute_plan(plan_id, confirmation, on_log=on_log)

    def save_custom_scene(self, scene: SceneDefinition) -> SceneDefinition:
        return self.scenes.save_custom(scene)

    def import_custom_scene(self, payload: str | dict[str, Any]) -> SceneDefinition:
        # 不在导入时创建或执行命令计划。
        return self.scenes.import_custom(payload)

    def export_custom_scene(self, scene_id: str) -> str:
        return self.scenes.export_custom(scene_id)

    def delete_custom_scene(self, scene_id: str) -> bool:
        return self.scenes.delete_custom(scene_id)
