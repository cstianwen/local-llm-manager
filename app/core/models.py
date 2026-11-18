"""核心数据结构：本地模型、运行中模型、使用模型的应用。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, TypeVar


_T = TypeVar("_T", bound="DictSerializable")


class DictSerializable:
    """数据类的字典导入导出；读取时忽略未来版本新增的未知键。"""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls: type[_T], data: dict[str, Any]) -> _T:
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})


@dataclass
class ModelInfo(DictSerializable):
    """一个被工具登记的本地大模型（文件或后端注册模型）。"""

    file_path: str
    file_name: str
    format: str                       # gguf / safetensors / pytorch_bin / ollama / unknown
    size_bytes: int
    architecture: str = ""             # 如 llama / qwen2 / mistral
    params_b: float = 0.0              # 参数量（十亿，B）
    quant: str = ""                   # 如 Q4_K_M
    source_dir: str = ""              # 扫描来源目录
    mtime: float = 0.0                # 文件修改时间
    provider: str = ""                # ollama / local-file
    model_ref: str = ""               # 后端内模型名（Ollama tag 等）
    discovered_at: float = 0.0
    model_id: str = ""
    display_name: str = ""
    context_length: int = 0
    license: str = ""
    integrity: str = "unknown"         # unknown / valid / invalid / checking
    integrity_error: str = ""
    source_type: str = ""              # file / backend / download 等来源分类
    shard_index: int = 0
    shard_count: int = 1
    shard_files: list[str] = field(default_factory=list)

    @property
    def size_human(self) -> str:
        return human_size(self.size_bytes)


@dataclass
class RunningModel(DictSerializable):
    """后端当前已加载到内存/显存的模型。"""

    name: str
    provider: str
    size_bytes: int = 0
    size_vram: int = 0
    expires_at: str = ""
    details: dict = field(default_factory=dict)

    @property
    def size_human(self) -> str:
        return human_size(self.size_bytes)

    @property
    def vram_human(self) -> str:
        return human_size(self.size_vram)


@dataclass
class AppUser(DictSerializable):
    """正在连接/使用推理服务的一个应用进程。"""

    pid: int
    name: str
    exe_path: str = ""
    cmdline: str = ""
    provider: str = ""
    target_port: int = 0
    model: str = ""


@dataclass
class OperationResult(DictSerializable):
    """服务操作的统一轻量返回值。"""

    success: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class DownloadProgress(DictSerializable):
    """下载任务进度快照。"""

    model_id: str = ""
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_bps: float = 0.0
    status: str = "pending"
    error: str = ""

    @property
    def percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, self.downloaded_bytes * 100.0 / self.total_bytes)


def human_size(n: int) -> str:
    """把字节数转成易读字符串。"""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PB"
