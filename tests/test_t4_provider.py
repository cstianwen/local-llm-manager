from types import SimpleNamespace

import pytest

import app.providers.t4 as t4_module
from app.core.errors import BackendOfflineError
from app.providers.base import CapabilityState
from app.providers.t4 import T4Provider


@pytest.fixture
def provider():
    instance = T4Provider()
    try:
        yield instance
    finally:
        instance.close()


def test_remote_and_lan_urls_are_rejected():
    with pytest.raises(ValueError, match="本机回环"):
        T4Provider("http://192.168.1.20:8712")
    with pytest.raises(ValueError, match="本机回环"):
        T4Provider("https://127.0.0.1:8712")


def test_gpu_status_only_allows_nonzero_tesla_t4(provider, monkeypatch):
    output = "\n".join([
        "0, GTX 1080, 8192, 1024, 7168",
        "1, Tesla T4, 15360, 2048, 13312",
        "2, Tesla T4, 15360, 4096, 11264",
        "3, GeForce RTX 4090, 24576, 100, 24476",
    ])
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(provider, "_nvidia_smi", lambda: r"C:\fake\nvidia-smi.exe")
    monkeypatch.setattr(t4_module.subprocess, "run", fake_run)
    result = provider.gpu_status()
    assert result["success"] is True
    assert [row["index"] for row in result["gpus"]] == [1, 2]
    assert [row["index"] for row in result["excluded"]] == [0, 3]
    assert result["gpus"][0]["memory_used_mib"] == 2048
    assert commands == [[
        r"C:\fake\nvidia-smi.exe",
        "--query-gpu=index,name,memory.total,memory.used,memory.free",
        "--format=csv,noheader,nounits",
    ]]


def test_detect_offline_never_executes_start_script(provider, monkeypatch):
    def offline():
        raise BackendOfflineError("offline")

    def forbidden_start(*args, **kwargs):
        raise AssertionError("detect must not start T4")

    monkeypatch.setattr(provider, "gpu_status", lambda: {"gpus": [], "excluded": []})
    monkeypatch.setattr(provider, "health", offline)
    monkeypatch.setattr(provider, "_start_script", forbidden_start)
    status = provider.detect()
    assert status.running is False
    assert status.details["fallback"]["automatic_start"] is False
    assert status.details["fallback"]["manual_start_required"] is True
    assert status.details["fallback"]["action"] == "NEEDS_START"
    assert "不会自动启动" in status.message


def test_health_reporting_gtx1080_is_not_usable(provider, monkeypatch):
    monkeypatch.setattr(provider, "gpu_status", lambda: {"gpus": [], "excluded": []})
    monkeypatch.setattr(provider, "health", lambda: {"ready": True, "gpu": "GTX 1080"})
    status = provider.detect()
    assert status.running is False
    assert status.state == CapabilityState.UNSUPPORTED
    assert "GTX1080" in status.error


def test_generate_uses_required_health_and_payload(provider):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def request_json(self, method, path, payload=None, **kwargs):
            self.calls.append((method, path, payload))
            if path == "/health":
                return {"ready": True, "model": "qwen-t4", "gpu": "Tesla T4"}
            assert path == "/generate"
            return {"text": "真实返回文本"}

    client = FakeClient()
    provider._client = client
    events = list(provider.generate(
        "qwen-t4", "请回答", options={"max_new_tokens": 64, "temperature": 0.25},
    ))
    assert events[0]["response"] == "真实返回文本"
    assert events[0]["done"] is True
    assert client.calls == [
        ("GET", "/health", None),
        ("POST", "/generate", {
            "prompt": "请回答", "max_new_tokens": 64, "temperature": 0.25,
        }),
    ]
