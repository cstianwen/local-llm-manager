"""模型文件识别与轻量格式检查，不依赖 torch、numpy 等第三方库。"""
from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path
from typing import Any

from ..core.config import MIN_MODEL_SIZE_BYTES, MODEL_EXTENSIONS
from .gguf import GGUFParseError, parse_gguf, quantization_from_filename
from .walker import ScannedFile

MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_SAFETENSORS_HEADER_BYTES = 64 * 1024 * 1024
MAX_TENSORS = 262_144
MAX_SHAPE_DIMS = 16
SHARD_PATTERN = re.compile(
    r"^(?P<base>.+)-(?P<index>\d{1,6})-of-(?P<count>\d{1,6})$", re.IGNORECASE
)
_UNKNOWN_NAME_HINTS = {
    "model", "weights", "pytorch", "llama", "qwen", "mistral", "gemma",
    "deepseek", "checkpoint", "stable-diffusion", "stable_diffusion", "phi-",
}
_DTYPE_SIZES = {
    "BOOL": 1, "U8": 1, "I8": 1, "F8_E4M3": 1, "F8_E5M2": 1, "F8_E8M0": 1,
    "I16": 2, "U16": 2, "F16": 2, "BF16": 2,
    "I32": 4, "U32": 4, "F32": 4,
    "I64": 8, "U64": 8, "F64": 8,
}


class ModelFormatError(ValueError):
    """模型文件内容无效或超出解析限制。"""


def is_index_file(path: str | Path) -> bool:
    name = Path(path).name.casefold()
    return name.endswith(".safetensors.index.json") or name.endswith(".bin.index.json")


def model_format(path: str | Path) -> str:
    name = Path(path).name.casefold()
    if name.endswith(".safetensors.index.json") or name.endswith(".safetensors"):
        return "safetensors"
    if name.endswith(".bin.index.json"):
        return "pytorch_bin"
    suffix = Path(path).suffix.casefold()
    if suffix == ".gguf":
        return "gguf"
    if suffix in {".bin", ".pt", ".ckpt"}:
        return "pytorch_bin"
    return "unknown"


def is_model_candidate(entry: ScannedFile, min_size: int = MIN_MODEL_SIZE_BYTES) -> bool:
    if is_index_file(entry.path):
        return True
    if entry.size < min_size:
        return False
    suffix = entry.path.suffix.casefold()
    if suffix in MODEL_EXTENSIONS:
        return True
    # 无扩展名/陌生扩展名必须同时满足更大阈值和模型命名特征，避免收录普通大文件。
    if entry.size < max(min_size, 512 * 1024 * 1024):
        return False
    name = entry.path.name.casefold()
    return any(hint in name for hint in _UNKNOWN_NAME_HINTS)


def display_name_from_path(path: str | Path) -> str:
    name = Path(path).name
    lower = name.casefold()
    for suffix in (".safetensors.index.json", ".bin.index.json"):
        if lower.endswith(suffix):
            name = name[:-len(suffix)]
            break
    else:
        name = Path(name).stem
    match = SHARD_PATTERN.match(name)
    if match:
        name = match.group("base")
    return name.strip(" ._-") or Path(path).name


def read_weight_map(path: str | Path) -> dict[str, str]:
    """读取 Safetensors/PyTorch index JSON 的 weight_map。"""
    target = Path(path)
    size = target.stat().st_size
    if size > MAX_JSON_BYTES:
        raise ModelFormatError(f"index JSON 过大：{size} 字节")
    try:
        with open(target, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelFormatError(f"index JSON 无效：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("weight_map"), dict):
        raise ModelFormatError("index JSON 缺少有效的 weight_map 对象")
    weight_map = payload["weight_map"]
    if not weight_map:
        raise ModelFormatError("index JSON 的 weight_map 为空")
    if len(weight_map) > MAX_TENSORS:
        raise ModelFormatError(f"index tensor 数量过多：{len(weight_map)}")
    result: dict[str, str] = {}
    for tensor_name, shard_name in weight_map.items():
        if not isinstance(tensor_name, str) or not tensor_name or len(tensor_name) > 4096:
            raise ModelFormatError("index weight_map 含无效 tensor 名称")
        if not isinstance(shard_name, str) or not shard_name or len(shard_name) > 4096:
            raise ModelFormatError(f"index tensor {tensor_name!r} 的分片名无效")
        result[tensor_name] = shard_name
    return result


def _metadata_string(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str):
            return value[:4096]
    return ""


def parse_safetensors(path: str | Path) -> dict[str, Any]:
    """校验 Safetensors JSON 头部、张量形状及数据偏移。"""
    target = Path(path)
    file_size = target.stat().st_size
    with open(target, "rb") as file:
        raw_length = file.read(8)
        if len(raw_length) != 8:
            raise ModelFormatError("Safetensors 头部不完整")
        header_length = struct.unpack("<Q", raw_length)[0]
        if header_length < 2 or header_length > MAX_SAFETENSORS_HEADER_BYTES:
            raise ModelFormatError(
                f"Safetensors 头部长度异常：{header_length}（上限 {MAX_SAFETENSORS_HEADER_BYTES}）"
            )
        if header_length + 8 > file_size:
            raise ModelFormatError("Safetensors 头部超出文件大小")
        raw_header = file.read(header_length)

    try:
        header = json.loads(raw_header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelFormatError(f"Safetensors JSON 头部无效：{exc}") from exc
    if not isinstance(header, dict):
        raise ModelFormatError("Safetensors 头部不是 JSON 对象")
    user_metadata = header.get("__metadata__", {})
    if not isinstance(user_metadata, dict):
        raise ModelFormatError("Safetensors __metadata__ 必须是对象")

    tensor_items = [(key, value) for key, value in header.items() if key != "__metadata__"]
    if len(tensor_items) > MAX_TENSORS:
        raise ModelFormatError(f"Safetensors tensor 数量过多：{len(tensor_items)}")
    data_size = file_size - 8 - header_length
    total_parameters = 0
    dtype_weights: dict[str, int] = {}

    for tensor_name, tensor in tensor_items:
        if not tensor_name or len(tensor_name) > 4096 or not isinstance(tensor, dict):
            raise ModelFormatError("Safetensors tensor 名称或目录项无效")
        dtype = tensor.get("dtype")
        if dtype not in _DTYPE_SIZES:
            raise ModelFormatError(f"未知 Safetensors 数据类型：{dtype!r}")
        shape = tensor.get("shape")
        offsets = tensor.get("data_offsets")
        if not isinstance(shape, list) or len(shape) > MAX_SHAPE_DIMS:
            raise ModelFormatError(f"Safetensors tensor {tensor_name!r} 的 shape 无效")
        elements = 1
        for dimension in shape:
            if not isinstance(dimension, int) or isinstance(dimension, bool) or not 0 <= dimension <= (1 << 40):
                raise ModelFormatError(f"Safetensors tensor {tensor_name!r} 的维度无效")
            elements *= dimension
        if (
            not isinstance(offsets, list) or len(offsets) != 2
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in offsets)
        ):
            raise ModelFormatError(f"Safetensors tensor {tensor_name!r} 的 data_offsets 无效")
        begin, end = offsets
        if begin > end or end > data_size:
            raise ModelFormatError(f"Safetensors tensor {tensor_name!r} 的数据范围越界")
        if end - begin != elements * _DTYPE_SIZES[dtype]:
            raise ModelFormatError(f"Safetensors tensor {tensor_name!r} 的形状与字节数不符")
        total_parameters += elements
        dtype_weights[dtype] = dtype_weights.get(dtype, 0) + elements

    context = user_metadata.get("context_length", user_metadata.get("max_position_embeddings", 0))
    if not isinstance(context, int) or isinstance(context, bool) or context < 0:
        context = 0
    ordered_dtypes = sorted(dtype_weights, key=lambda item: (-dtype_weights[item], item))
    return {
        "architecture": _metadata_string(user_metadata, "general.architecture", "architecture", "model_type"),
        "name": _metadata_string(user_metadata, "general.name", "name", "model_name"),
        "params_b": round(total_parameters / 1e9, 6) if total_parameters else 0.0,
        "context_length": context,
        "quant": "+".join(ordered_dtypes),
        "quant_source": "tensor" if ordered_dtypes else "",
        "license": _metadata_string(user_metadata, "license", "general.license"),
        "integrity": "valid",
        "integrity_error": "",
    }


def _inspect_pytorch(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as file:
        magic = file.read(8)
    recognised = magic.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x80"))
    return {
        "integrity": "valid" if recognised else "unknown",
        "integrity_error": "",
    }


def _empty_metadata() -> dict[str, Any]:
    return {
        "architecture": "", "name": "", "params_b": 0.0, "context_length": 0,
        "quant": "", "quant_source": "", "license": "",
        "integrity": "unknown", "integrity_error": "", "_cacheable": True,
    }


def inspect_file(path: str | Path) -> dict[str, Any]:
    """把单文件解析失败转成模型级错误，绝不让一个坏文件中断扫描。"""
    result = _empty_metadata()
    try:
        file_format = model_format(path)
        if file_format == "gguf":
            result.update(parse_gguf(path))
        elif file_format == "safetensors":
            result.update(parse_safetensors(path))
        elif file_format == "pytorch_bin":
            result.update(_inspect_pytorch(path))
    except (GGUFParseError, ModelFormatError) as exc:
        result["integrity"] = "invalid"
        result["integrity_error"] = str(exc)
    except OSError as exc:
        result["integrity"] = "invalid"
        result["integrity_error"] = f"无法读取文件：{exc}"
        result["_cacheable"] = False
    except Exception as exc:
        result["integrity"] = "invalid"
        result["integrity_error"] = f"模型头解析失败：{exc}"

    if not result.get("quant"):
        guessed = quantization_from_filename(path)
        if guessed:
            result["quant"] = guessed
            result["quant_source"] = "filename"
    return result


def quant_label(metadata: dict[str, Any]) -> str:
    quant = str(metadata.get("quant", "") or "")
    if quant and metadata.get("quant_source") == "filename":
        return f"{quant}（文件名推测）"
    return quant
