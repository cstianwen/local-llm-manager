"""端口、Ollama 日志与文件占用反查公开 API。"""
from .base import Confidence, InspectionResult, resolve_process
from .files import FileInspector, RestartManagerInspector, file_lock_users, find_file_users
from .ollama_logs import (
    OllamaInspectionResult,
    OllamaInspector,
    OllamaLogEntry,
    OllamaLogInspector,
    default_ollama_log_paths,
    find_ollama_users,
    inspect_ollama_logs,
)
from .ports import PortInspector, find_port_users, port_to_pids


class Inspector:
    """便于应用服务组合三类只读反查，不自动终止任何进程。"""

    def __init__(self):
        self.ports = PortInspector()
        self.files = RestartManagerInspector()
        self.ollama = OllamaInspector(OllamaLogInspector(port_inspector=self.ports))

    def inspect_port(self, port: int, **options) -> InspectionResult:
        return self.ports.inspect(port, **options)

    def inspect_file(self, path, **options) -> InspectionResult:
        return self.files.inspect(path, **options)

    def inspect_ollama(self, model: str = "", port: int | None = 11434) -> OllamaInspectionResult:
        return self.ollama.inspect(model=model, port=port)


__all__ = [
    "Confidence", "InspectionResult", "Inspector", "PortInspector", "FileInspector",
    "RestartManagerInspector", "OllamaInspector", "OllamaLogInspector", "OllamaLogEntry",
    "OllamaInspectionResult", "find_port_users", "port_to_pids", "find_file_users",
    "file_lock_users", "find_ollama_users", "inspect_ollama_logs", "default_ollama_log_paths",
    "resolve_process",
]
