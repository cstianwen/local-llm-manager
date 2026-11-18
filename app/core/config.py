"""全局配置：后端地址、扫描阈值、跳过目录、缓存路径。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "本地模型管家"
APP_VERSION = "1.0.0"

# ---- 后端默认连接（仅定义地址，不在导入时发起连接）----
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1")
OLLAMA_PORT = int(os.environ.get("OLLAMA_PORT", "11434"))
_DEFAULT_OLLAMA_URL = (
    OLLAMA_HOST if "://" in OLLAMA_HOST
    else f"http://{OLLAMA_HOST}:{OLLAMA_PORT}"
)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", _DEFAULT_OLLAMA_URL).rstrip("/")

# ---- 本地日志与数据库 ----
LOG_FILE_NAME = "llm-manager.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
DB_BUSY_TIMEOUT_MS = 5000

# ---- 扫描阈值 ----
# 小于该大小的文件直接忽略（默认 50 MB）
MIN_MODEL_SIZE_BYTES = 50 * 1024 * 1024

# 全盘扫描时跳过的系统/无关目录名（按目录名匹配，不区分大小写）
SKIP_DIR_NAMES = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "winreagent",
    "appdata", "$windows.~ws", "$windows.~bt", "msocache",
}

# 常见模型文件扩展名
MODEL_EXTENSIONS = {".gguf", ".bin", ".safetensors", ".ggml", ".onnx", ".pt", ".ckpt"}

# ---- 运行状态轮询间隔（毫秒）----
STATUS_POLL_INTERVAL_MS = 5000


def is_windows() -> bool:
    return sys.platform.startswith("win")


def app_data_dir() -> Path:
    """返回应用缓存/数据目录，Windows 用 %APPDATA%，其它平台用 ~/.llm-manager。"""
    if is_windows():
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        p = Path(base) / "LLMManager"
    else:
        p = Path.home() / ".llm-manager"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return app_data_dir() / "models.db"


def log_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    return log_dir() / LOG_FILE_NAME


def ollama_models_root() -> Path:
    """Ollama 默认模型存储目录（Windows）。"""
    if is_windows():
        home = os.environ.get("USERPROFILE", str(Path.home()))
        return Path(home) / ".ollama" / "models"
    return Path.home() / ".ollama" / "models"
