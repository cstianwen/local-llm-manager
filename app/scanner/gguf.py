"""GGUF 元数据与张量目录解析（只读头部，不加载权重）。"""
from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any, BinaryIO

GGUF_MAGIC = b"GGUF"
MAX_STRING_BYTES = 16 * 1024 * 1024
MAX_KEY_BYTES = 4096
MAX_ARRAY_ITEMS = 1_048_576
MAX_TOTAL_ARRAY_ITEMS = 2_097_152
MAX_KV_COUNT = 65_536
MAX_TENSOR_COUNT = 262_144
MAX_TENSOR_DIMS = 4
MAX_HEADER_BYTES = 256 * 1024 * 1024

# GGUF metadata value types。
_U8, _I8, _U16, _I16, _U32, _I32, _F32, _BOOL, _STR, _ARR, _U64, _I64, _F64 = range(13)
_SCALAR_TYPES = {
    _U8, _I8, _U16, _I16, _U32, _I32, _F32, _BOOL, _STR, _U64, _I64, _F64,
}

# ggml 张量类型编号是 GGUF 规范的一部分，不使用 general.quantization_version 猜测量化。
_TENSOR_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 4: "Q4_2", 5: "Q4_3",
    6: "Q5_0", 7: "Q5_1", 8: "Q8_0", 9: "Q8_1", 10: "Q2_K",
    11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 15: "Q8_K",
    16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS", 19: "IQ1_S",
    20: "IQ4_NL", 21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS",
    24: "I8", 25: "I16", 26: "I32", 27: "I64", 28: "F64",
    29: "IQ1_M", 30: "BF16", 31: "Q4_0_4_4", 32: "Q4_0_4_8",
    33: "Q4_0_8_8", 34: "TQ1_0", 35: "TQ2_0", 36: "IQ4_NL_4_4",
    37: "IQ4_NL_4_8", 38: "IQ4_NL_8_8",
}

_QUANT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(IQ\d+(?:_[A-Za-z0-9]+)+|Q\d+_(?:K(?:_[MS])?|0|1)|F16|BF16|F32)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


class GGUFParseError(ValueError):
    """GGUF 内容不符合规范或超过安全限制。"""


def quantization_from_filename(path: str | Path) -> str:
    match = _QUANT_PATTERN.search(Path(path).stem)
    return match.group(1).upper() if match else ""


class _Reader:
    def __init__(self, file: BinaryIO):
        self.file = file
        self.position = 0
        self.array_items = 0

    def _read(self, size: int) -> bytes:
        if size < 0 or self.position + size > MAX_HEADER_BYTES:
            raise GGUFParseError("GGUF 头部超过允许大小")
        data = self.file.read(size)
        if len(data) != size:
            raise GGUFParseError(f"GGUF 头部不完整：需要 {size} 字节，实际 {len(data)} 字节")
        self.position += size
        return data

    def u32(self) -> int:
        return struct.unpack("<I", self._read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self._read(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self._read(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self._read(8))[0]

    def string(self, limit: int = MAX_STRING_BYTES) -> str:
        size = self.u64()
        if size > limit:
            raise GGUFParseError(f"GGUF 字符串过长：{size} 字节（上限 {limit}）")
        raw = self._read(size)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GGUFParseError(f"GGUF 字符串不是有效 UTF-8：{exc}") from exc

    def value(self, value_type: int) -> Any:
        if value_type not in _SCALAR_TYPES and value_type != _ARR:
            raise GGUFParseError(f"未知 GGUF 元数据类型：{value_type}")
        if value_type == _U8:
            return self._read(1)[0]
        if value_type == _I8:
            return struct.unpack("<b", self._read(1))[0]
        if value_type == _U16:
            return struct.unpack("<H", self._read(2))[0]
        if value_type == _I16:
            return struct.unpack("<h", self._read(2))[0]
        if value_type == _U32:
            return self.u32()
        if value_type == _I32:
            return struct.unpack("<i", self._read(4))[0]
        if value_type == _F32:
            return self.f32()
        if value_type == _BOOL:
            value = self._read(1)[0]
            if value not in (0, 1):
                raise GGUFParseError(f"GGUF 布尔值无效：{value}")
            return bool(value)
        if value_type == _STR:
            return self.string()
        if value_type == _U64:
            return self.u64()
        if value_type == _I64:
            return struct.unpack("<q", self._read(8))[0]
        if value_type == _F64:
            return self.f64()

        element_type = self.u32()
        if element_type not in _SCALAR_TYPES:
            if element_type == _ARR:
                raise GGUFParseError("GGUF 不允许嵌套数组")
            raise GGUFParseError(f"未知 GGUF 数组元素类型：{element_type}")
        count = self.u64()
        self.array_items += count
        if count > MAX_ARRAY_ITEMS or self.array_items > MAX_TOTAL_ARRAY_ITEMS:
            raise GGUFParseError(
                f"GGUF 数组元素过多：{count}（单数组上限 {MAX_ARRAY_ITEMS}，"
                f"总上限 {MAX_TOTAL_ARRAY_ITEMS}）"
            )
        return [self.value(element_type) for _ in range(count)]


def _positive_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def parse_gguf(path: str | Path) -> dict[str, Any]:
    """解析真实 GGUF 元数据；损坏或未知类型会抛出 ``GGUFParseError``。"""
    metadata: dict[str, Any] = {}
    tensor_types: dict[str, int] = {}
    total_parameters = 0

    with open(path, "rb") as file:
        reader = _Reader(file)
        if reader._read(4) != GGUF_MAGIC:
            raise GGUFParseError("文件头不是 GGUF")
        version = reader.u32()
        if version not in (1, 2, 3):
            raise GGUFParseError(f"不支持的 GGUF 版本：{version}")
        tensor_count = reader.u64()
        kv_count = reader.u64()
        if tensor_count > MAX_TENSOR_COUNT:
            raise GGUFParseError(f"GGUF tensor 数量异常：{tensor_count}（上限 {MAX_TENSOR_COUNT}）")
        if kv_count > MAX_KV_COUNT:
            raise GGUFParseError(f"GGUF KV 数量异常：{kv_count}（上限 {MAX_KV_COUNT}）")

        for _ in range(kv_count):
            key = reader.string(MAX_KEY_BYTES)
            value_type = reader.u32()
            metadata[key] = reader.value(value_type)

        for _ in range(tensor_count):
            tensor_name = reader.string(MAX_KEY_BYTES)
            if not tensor_name:
                raise GGUFParseError("GGUF tensor 名称不能为空")
            dimensions_count = reader.u32()
            if dimensions_count > MAX_TENSOR_DIMS:
                raise GGUFParseError(
                    f"GGUF tensor 维度异常：{dimensions_count}（上限 {MAX_TENSOR_DIMS}）"
                )
            dimensions = [reader.u64() for _ in range(dimensions_count)]
            tensor_type = reader.u32()
            if tensor_type not in _TENSOR_TYPES:
                raise GGUFParseError(f"未知 GGUF tensor 类型：{tensor_type}")
            reader.u64()  # 权重相对数据区的偏移，本次不读取权重。
            element_count = 1
            for dimension in dimensions:
                if dimension > (1 << 40):
                    raise GGUFParseError(f"GGUF tensor 维度大小异常：{dimension}")
                element_count *= dimension
            total_parameters += element_count
            type_name = _TENSOR_TYPES[tensor_type]
            tensor_types[type_name] = tensor_types.get(type_name, 0) + element_count

    architecture = metadata.get("general.architecture", "")
    if not isinstance(architecture, str):
        architecture = ""
    name = metadata.get("general.name", metadata.get("general.basename", ""))
    if not isinstance(name, str):
        name = ""
    context = _positive_int(metadata.get(f"{architecture}.context_length"))
    if not context:
        context = _positive_int(metadata.get("general.context_length"))
    license_name = metadata.get("general.license", "")
    if not isinstance(license_name, str):
        license_name = ""

    ordered_types = sorted(tensor_types, key=lambda item: (-tensor_types[item], item))
    quant = "+".join(ordered_types)
    quant_source = "tensor" if quant else ""
    if not quant:
        quant = quantization_from_filename(path)
        quant_source = "filename" if quant else ""

    return {
        "architecture": architecture,
        "name": name,
        "params_b": round(total_parameters / 1e9, 6) if total_parameters else 0.0,
        "context_length": context,
        "quant": quant,
        "quant_source": quant_source,
        "license": license_name,
        "integrity": "valid",
        "integrity_error": "",
    }
