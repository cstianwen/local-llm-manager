"""版本化 JSON 清单/配置导入导出，以及真实 llama.cpp 格式转换委托。"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ..core.models import ModelInfo, OperationResult
from ..core.processes import ManagedProcess
from ..providers.registry import ProviderRegistry
from ..store.db import ModelStore

MODEL_SCHEMA = "local-model-manager/model-manifest"
CONFIG_SCHEMA = "local-model-manager/config"
TRANSFER_VERSION = 1
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_MODELS = 20_000


def _read_json(source: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(source).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"导入文件不存在：{path}")
    size = path.stat().st_size
    if size > _MAX_JSON_BYTES:
        raise ValueError(f"导入 JSON 超过 {_MAX_JSON_BYTES} 字节安全限制")
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"导入文件不是有效 UTF-8 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("导入 JSON 顶层必须是对象")
    if payload.get("version") != TRANSFER_VERSION or isinstance(payload.get("version"), bool):
        raise ValueError(f"不支持的导入版本：{payload.get('version')!r}；当前仅支持 {TRANSFER_VERSION}")
    return payload


def _write_json(destination: str | os.PathLike[str], payload: dict[str, Any]) -> Path:
    path = Path(destination).expanduser().absolute()
    if path.exists() and path.is_dir():
        raise IsADirectoryError(f"导出目标是目录：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _schema(payload: dict[str, Any], expected: str) -> None:
    if payload.get("schema") != expected:
        raise ValueError(f"JSON schema 不匹配：需要 {expected}，实际为 {payload.get('schema')!r}")


def _model_from_import(value: Any) -> ModelInfo:
    if not isinstance(value, dict):
        raise ValueError("models 数组中的每一项都必须是对象")
    for field in ("file_path", "file_name", "format"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"模型记录缺少有效字符串字段：{field}")
    size = value.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError("模型 size_bytes 必须是非负整数")
    if "params_b" in value and (not isinstance(value["params_b"], (int, float)) or isinstance(value["params_b"], bool) or value["params_b"] < 0):
        raise ValueError("模型 params_b 必须是非负数")
    if "shard_files" in value and (
        not isinstance(value["shard_files"], list)
        or any(not isinstance(item, str) for item in value["shard_files"])
    ):
        raise ValueError("模型 shard_files 必须是字符串数组")
    try:
        model = ModelInfo.from_dict(value)
    except TypeError as exc:
        raise ValueError(f"模型记录字段无效：{exc}") from exc
    if not model.model_id:
        identity = f"{model.provider}\0{model.file_path}".encode("utf-8")
        model.model_id = "import-" + hashlib.sha256(identity).hexdigest()[:24]
    if model.provider == "local-file":
        # 导入记录不是本机扫描证据，安全隔离服务会拒绝据其移动文件。
        model.source_type = "import"
    return model


def _validate_preferences(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("preferences 必须是对象")
    result: dict[str, dict[str, Any]] = {}
    for model_id, item in value.items():
        if not isinstance(model_id, str) or not isinstance(item, dict):
            raise ValueError("preferences 的键/值格式无效")
        changes = {key: item[key] for key in ("favorite", "pinned", "tags", "notes") if key in item}
        if "tags" in changes and (not isinstance(changes["tags"], list) or any(not isinstance(tag, str) for tag in changes["tags"])):
            raise ValueError("模型偏好 tags 必须是字符串数组")
        if "notes" in changes and not isinstance(changes["notes"], str):
            raise ValueError("模型偏好 notes 必须是字符串")
        result[model_id] = changes
    return result


class TransferService:
    """导入只解析并可选写入 SQLite；没有命令、扫描、安装或网络执行路径。"""

    def __init__(self, store: ModelStore):
        self.store = store

    def export_models(
        self,
        destination: str | os.PathLike[str],
        *,
        model_ids: list[str] | tuple[str, ...] | None = None,
        include_preferences: bool = True,
    ) -> OperationResult:
        selected = None if model_ids is None else set(model_ids)
        models = [model for model in self.store.all() if selected is None or model.model_id in selected]
        if selected is not None:
            missing = selected - {model.model_id for model in models}
            if missing:
                raise KeyError(f"以下模型 ID 不存在：{', '.join(sorted(missing))}")
        preferences = {
            model.model_id: self.store.prefs.get(model.model_id)
            for model in models if include_preferences and model.model_id
        }
        payload = {
            "schema": MODEL_SCHEMA, "version": TRANSFER_VERSION, "exported_at": time.time(),
            "models": [model.to_dict() for model in models], "preferences": preferences,
        }
        path = _write_json(destination, payload)
        self.store.operations.add("manifest.export", str(path), details={"model_count": len(models)})
        return OperationResult(True, "模型清单已导出；文件仅为 JSON 数据。", data={"path": str(path), "model_count": len(models)})

    def export_config(self, destination: str | os.PathLike[str]) -> OperationResult:
        settings = {
            key: value for key, value in self.store.settings.all().items()
            if not key.startswith(("scanner.parse_cache", "catalog.cache"))
        }
        preset_rows = self.store.connection.execute("SELECT * FROM load_presets ORDER BY name").fetchall()
        presets = []
        for row in preset_rows:
            item = dict(row)
            try:
                item["options"] = json.loads(item["options"] or "{}")
            except (TypeError, ValueError):
                item["options"] = {}
            item.pop("id", None)
            presets.append(item)
        payload = {
            "schema": CONFIG_SCHEMA, "version": TRANSFER_VERSION, "exported_at": time.time(),
            "settings": settings, "load_presets": presets,
            "scan_roots": self.store.scan_roots.list(),
            "notice": "配置仅是数据。本工具导入时不会执行其中的命令或自动扫描。",
        }
        path = _write_json(destination, payload)
        self.store.operations.add("config.export", str(path), details={"preset_count": len(presets), "scan_root_count": len(payload["scan_roots"])})
        return OperationResult(True, "配置已导出；导入配置不会自动执行任何命令。", data={"path": str(path)})

    def import_models(self, source: str | os.PathLike[str], *, apply: bool = False) -> OperationResult:
        payload = _read_json(source)
        _schema(payload, MODEL_SCHEMA)
        return self._import_models(payload, apply=apply)

    def _import_models(self, payload: dict[str, Any], *, apply: bool) -> OperationResult:
        values = payload.get("models")
        if not isinstance(values, list) or len(values) > _MAX_MODELS:
            raise ValueError(f"models 必须是数组且最多包含 {_MAX_MODELS} 项")
        models = [_model_from_import(item) for item in values]
        preferences = _validate_preferences(payload.get("preferences", {}))
        unknown_prefs = set(preferences) - {model.model_id for model in models}
        if unknown_prefs:
            raise ValueError("preferences 含清单中不存在的模型 ID")
        if apply:
            self.store.upsert_many(models)
            for model_id, changes in preferences.items():
                self.store.prefs.set(model_id, **changes)
        self.store.operations.add(
            "manifest.import", status="success",
            message="模型清单已写入；未执行其中任何内容。" if apply else "模型清单预览完成；未写入数据库或执行任何内容。",
            details={"model_count": len(models), "applied": apply},
        )
        return OperationResult(
            True, "模型清单已导入；未执行任何命令。" if apply else "模型清单校验通过；当前仅为预览，未写入数据库。",
            data={"kind": "models", "applied": apply, "model_count": len(models), "models": [model.to_dict() for model in models]},
        )

    def import_config(self, source: str | os.PathLike[str], *, apply: bool = False) -> OperationResult:
        payload = _read_json(source)
        _schema(payload, CONFIG_SCHEMA)
        settings = payload.get("settings")
        presets = payload.get("load_presets", [])
        roots = payload.get("scan_roots", [])
        if not isinstance(settings, dict) or any(not isinstance(key, str) or not key for key in settings):
            raise ValueError("settings 必须是非空字符串键组成的对象")
        if not isinstance(presets, list) or not isinstance(roots, list):
            raise ValueError("load_presets 和 scan_roots 必须是数组")
        validated_presets = []
        for item in presets:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise ValueError("每个装入预设必须含非空 name")
            options = item.get("options", {})
            if not isinstance(options, dict):
                raise ValueError(f"预设 {item['name']} 的 options 必须是对象；导入不会执行其中内容")
            validated_presets.append({
                "name": item["name"], "model_id": str(item.get("model_id", "")),
                "provider": str(item.get("provider", "")), "device": str(item.get("device", "auto")),
                "context_length": self._nonnegative_int(item.get("context_length", 0), "context_length"),
                "gpu_layers": self._integer(item.get("gpu_layers", 0), "gpu_layers"),
                "threads": self._nonnegative_int(item.get("threads", 0), "threads"),
                "options": options,
            })
        validated_roots = []
        for item in roots:
            if isinstance(item, str):
                path, enabled = item, True
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                path, enabled = item["path"], bool(item.get("enabled", True))
            else:
                raise ValueError("scan_roots 项必须是路径字符串或含 path 的对象")
            if not path.strip() or "\x00" in path:
                raise ValueError("扫描根路径无效")
            validated_roots.append((path, enabled))
        if apply:
            self.store.settings.update(settings)
            now = time.time()
            with self.store.transaction() as conn:
                for item in validated_presets:
                    conn.execute(
                        """INSERT INTO load_presets(
                               name, model_id, provider, device, context_length,
                               gpu_layers, threads, options, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(name) DO UPDATE SET
                               model_id=excluded.model_id, provider=excluded.provider,
                               device=excluded.device, context_length=excluded.context_length,
                               gpu_layers=excluded.gpu_layers, threads=excluded.threads,
                               options=excluded.options, updated_at=excluded.updated_at""",
                        (
                            item["name"], item["model_id"], item["provider"], item["device"],
                            item["context_length"], item["gpu_layers"], item["threads"],
                            json.dumps(item["options"], ensure_ascii=False), now, now,
                        ),
                    )
            for path, enabled in validated_roots:
                self.store.scan_roots.add(path, enabled)
        self.store.operations.add(
            "config.import", message="配置已写入；未执行命令或启动扫描。" if apply else "配置预览完成；未写入或执行任何内容。",
            details={"applied": apply, "preset_count": len(validated_presets), "scan_root_count": len(validated_roots)},
        )
        return OperationResult(
            True, "配置已导入；未执行任何命令或自动扫描。" if apply else "配置校验通过；当前仅为预览，未写入数据库。",
            data={
                "kind": "config", "applied": apply, "settings": settings,
                "load_presets": validated_presets,
                "scan_roots": [{"path": path, "enabled": enabled} for path, enabled in validated_roots],
            },
        )

    @staticmethod
    def _integer(value: Any, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} 必须是整数")
        return value

    @staticmethod
    def _nonnegative_int(value: Any, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} 必须是非负整数")
        return value

    def import_document(self, source: str | os.PathLike[str], *, apply: bool = False) -> OperationResult:
        payload = _read_json(source)
        schema = payload.get("schema")
        if schema == MODEL_SCHEMA:
            return self._import_models(payload, apply=apply)
        if schema == CONFIG_SCHEMA:
            # 复用同一个严格校验入口，避免第二套宽松解析。
            temporary = self.import_config(source, apply=apply)
            return temporary
        raise ValueError(f"未知导入 schema：{schema!r}")


class FormatConversionService:
    """格式/量化转换仅委托 Provider 已检测到的 llama.cpp 工具。"""

    def __init__(self, store: ModelStore, registry: ProviderRegistry, provider_name: str = "llamacpp"):
        self.store = store
        self.registry = registry
        self.provider_name = provider_name

    def convert(
        self,
        input_path: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        quantization: str,
        *,
        threads: int | None = None,
        model_id: str = "",
    ) -> ManagedProcess:
        provider = self.registry.get(self.provider_name)
        quantize = getattr(provider, "quantize", None)
        if not callable(quantize):
            raise ValueError("当前 llama.cpp Provider 没有真实量化接口")
        executable = str(getattr(provider, "quantizer_executable", "") or "")
        if not executable:
            raise FileNotFoundError("未检测到 llama-quantize/quantize；未执行伪造格式转换")
        process = quantize(input_path, output_path, quantization, threads=threads)
        self.store.operations.add(
            "model.convert", model_id or os.fspath(input_path),
            message="已启动本工具监管的真实 llama.cpp 量化进程。",
            details={"executable": executable, "output_path": os.fspath(output_path), "process": process.to_dict()},
        )
        return process


ModelTransferService = TransferService
ConversionService = FormatConversionService
