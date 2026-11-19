import pytest

from app.agent.engine import AgentEngine
from app.agent.errors import AgentError
from app.agent.models import ScenePhase
from app.core.errors import BackendOfflineError
from app.core.models import ModelInfo, OperationResult, RunningModel
from app.providers.base import (
    CapabilityState,
    Provider,
    ProviderAPIError,
    ProviderStatus,
)


class FakeProvider(Provider):
    def __init__(self, name, model, *, running=True):
        self.name = name
        self.display_name = name
        self.model = model
        self.running = running
        self.detect_calls = 0
        self.chat_calls = 0
        self.start_calls = 0
        self.chat_events = []
        self.chat_error = None
        self.start_script = r"C:\fake\t4\start_t4.ps1"

    def detect(self):
        self.detect_calls += 1
        if not self.running:
            raise BackendOfflineError(f"{self.name} offline")
        return ProviderStatus(
            provider=self.name,
            installed=True,
            running=True,
            state=CapabilityState.SUPPORTED,
        )

    def capabilities(self):
        return {}

    def list_models(self):
        if not self.running:
            raise BackendOfflineError(f"{self.name} offline")
        return [ModelInfo(
            file_path=f"{self.name}://{self.model}", file_name=self.model,
            format="unknown", size_bytes=0, provider=self.name, model_ref=self.model,
        )]

    def running_models(self):
        return [RunningModel(name=self.model, provider=self.name)] if self.running else []

    def chat(self, model, messages, **kwargs):
        self.chat_calls += 1
        for event in self.chat_events:
            yield event
        if self.chat_error is not None:
            raise self.chat_error

    def start_service(self, *args, **kwargs):
        self.start_calls += 1
        self.running = True
        return OperationResult(True, "fake start")


def make_engine(primary, fallback):
    return AgentEngine(primary, fallback_provider=fallback)


def test_ollama_remains_default_without_touching_t4():
    primary = FakeProvider("ollama", "qwen")
    fallback = FakeProvider("t4", "qwen-t4")
    state = make_engine(primary, fallback).prepare_scene("general-agent", model="qwen")
    assert state.phase == ScenePhase.READY
    assert state.model == "qwen"
    assert state.data.get("active_provider") is None
    assert fallback.detect_calls == 0
    assert fallback.start_calls == 0


def test_ready_t4_is_used_only_after_ollama_failure():
    primary = FakeProvider("ollama", "qwen", running=False)
    fallback = FakeProvider("t4", "qwen-t4")
    state = make_engine(primary, fallback).prepare_scene("general-agent", model="qwen")
    assert state.phase == ScenePhase.READY
    assert state.model == "qwen-t4"
    assert state.data["active_provider"] == "t4"
    assert state.data["fallback"] is True
    assert fallback.start_calls == 0


def test_offline_fallback_requires_manual_start():
    primary = FakeProvider("ollama", "qwen", running=False)
    fallback = FakeProvider("t4", "qwen-t4", running=False)
    state = make_engine(primary, fallback).prepare_scene("general-agent", model="qwen")
    assert state.phase == ScenePhase.PROVIDER_OFFLINE
    assert state.data["code"] == "NEEDS_START"
    assert state.data["manual_start_required"] is True
    assert "不会自动执行脚本" in state.message
    assert fallback.start_calls == 0


def test_stream_failure_falls_back_without_mixing_answers():
    primary = FakeProvider("ollama", "qwen")
    primary.chat_error = BackendOfflineError("stream offline")
    fallback = FakeProvider("t4", "qwen-t4")
    fallback.chat_events = [
        {"message": {"role": "assistant", "content": "T4 "}, "done": False},
        {"message": {"role": "assistant", "content": "回复"}, "done": True},
    ]
    engine = make_engine(primary, fallback)
    session = engine.create_session("general-agent", model="qwen")
    assistant = engine.reply(session.id, "你好")
    assert assistant.content == "T4 回复"
    assert assistant.status == "completed"
    assert assistant.model == "qwen-t4"
    assert assistant.metadata["active_provider"] == "t4"
    assert primary.chat_calls == 1
    assert fallback.chat_calls == 1
    assert fallback.start_calls == 0


def test_partial_ollama_text_is_never_completed_by_t4():
    primary = FakeProvider("ollama", "qwen")
    primary.chat_events = [
        {"message": {"role": "assistant", "content": "半截"}, "done": False},
    ]
    primary.chat_error = ProviderAPIError("broken stream", provider="ollama")
    fallback = FakeProvider("t4", "qwen-t4")
    fallback.chat_events = [
        {"message": {"role": "assistant", "content": "另一答案"}, "done": True},
    ]
    engine = make_engine(primary, fallback)
    session = engine.create_session("general-agent", model="qwen")
    with pytest.raises(AgentError, match="broken stream"):
        engine.reply(session.id, "你好")
    assert fallback.detect_calls == 0
    assert fallback.chat_calls == 0
    assert fallback.start_calls == 0
