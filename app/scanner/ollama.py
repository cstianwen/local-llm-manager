"""只读解析本机 Ollama manifests 与共享 blobs 的映射，不发起网络请求。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import ollama_models_root
from ..core.models import ModelInfo
from .walker import DirectoryWalker, ScannedFile, check_cancelled

_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_LAYERS = 4096
_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


@dataclass
class OllamaScanResult:
    models: list[ModelInfo] = field(default_factory=list)
    issues: list[tuple[str, str]] = field(default_factory=list)
    models_root: Path = field(default_factory=ollama_models_root)
    traversal_complete: bool = True


def _models_root(root: str | Path | None) -> Path:
    if root is None:
        return Path(ollama_models_root())
    base = Path(os.path.abspath(os.fspath(root)))
    if base.name.casefold() == "models" or (base / "manifests").is_dir():
        return base
    return base / "models"


def _model_ref(manifest: Path, manifests_root: Path) -> str:
    try:
        parts = manifest.relative_to(manifests_root).parts
    except ValueError:
        return manifest.stem
    if len(parts) >= 3:
        name_parts = list(parts[1:-1])
        if name_parts[:1] == ["library"]:
            name_parts = name_parts[1:]
        if name_parts:
            return f"{'/'.join(name_parts)}:{parts[-1]}"
    return manifest.stem


def _model_id(model_ref: str, manifest: Path) -> str:
    identity = model_ref or os.path.normcase(os.fspath(manifest))
    digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:24]
    return f"ollama-{digest}"


def _blob_path(models_root: Path, digest: str) -> Path | None:
    match = _DIGEST_PATTERN.fullmatch(digest)
    if not match:
        return None
    return models_root / "blobs" / f"sha256-{match.group(1).casefold()}"


def _declared_size(descriptor: Any) -> int:
    if not isinstance(descriptor, dict):
        return 0
    value = descriptor.get("size", 0)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _read_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    size = path.stat().st_size
    if size > _MAX_MANIFEST_BYTES:
        raise ValueError(f"Ollama 配置元数据过大：{size} 字节")
    with open(path, "r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("Ollama 配置元数据不是 JSON 对象")
    return payload


def _params_b(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)\s*B\b", value, re.IGNORECASE)
    return float(match.group(1)) if match else 0.0


def _base_model(manifest: ScannedFile, models_root: Path, manifests_root: Path) -> ModelInfo:
    reference = _model_ref(manifest.path, manifests_root)
    return ModelInfo(
        file_path=str(manifest.path), file_name=manifest.path.name,
        format="ollama", size_bytes=0, source_dir=str(models_root),
        mtime=manifest.mtime, provider="ollama", model_ref=reference,
        discovered_at=time.time(), model_id=_model_id(reference, manifest.path),
        display_name=reference or manifest.path.stem, integrity="unknown",
        source_type="backend", shard_index=0, shard_count=0, shard_files=[],
    )


def scan_ollama(root: str | Path | None = None, token: Any = None) -> OllamaScanResult:
    """扫描 Ollama 注册模型。仅 stat/读取 manifest 和小型配置，绝不修改共享 blob。"""
    models_root = _models_root(root)
    manifests_root = models_root / "manifests"
    result = OllamaScanResult(models_root=models_root)
    if not manifests_root.exists():
        return result

    walker = DirectoryWalker(skip_dir_names=set())
    manifests: list[ScannedFile] = []
    for entry in walker.walk(manifests_root, token):
        manifests.append(entry)
    result.issues.extend(walker.issues)
    result.traversal_complete = not walker.issues

    for manifest in sorted(manifests, key=lambda item: os.path.normcase(os.fspath(item.path))):
        check_cancelled(token)
        model = _base_model(manifest, models_root, manifests_root)
        try:
            if manifest.size > _MAX_MANIFEST_BYTES:
                raise ValueError(f"Ollama manifest 过大：{manifest.size} 字节")
            with open(manifest.path, "r", encoding="utf-8") as file:
                payload = json.load(file)
            if not isinstance(payload, dict) or not isinstance(payload.get("layers"), list):
                raise ValueError("Ollama manifest 缺少有效的 layers 数组")
            layers = payload["layers"]
            if len(layers) > _MAX_LAYERS:
                raise ValueError(f"Ollama layer 数量过多：{len(layers)}")
            config_descriptor = payload.get("config", {})
            descriptors = [config_descriptor, *layers]
            if any(not isinstance(item, dict) for item in descriptors):
                raise ValueError("Ollama manifest 含无效 config/layer 项")

            missing = False
            invalid = False
            errors: list[str] = []
            blob_entries: dict[str, ScannedFile | None] = {}
            blob_paths: dict[str, Path | None] = {}
            for descriptor in descriptors:
                digest = descriptor.get("digest", "")
                if not isinstance(digest, str):
                    invalid = True
                    errors.append("manifest 含无效 digest")
                    continue
                path = _blob_path(models_root, digest)
                blob_paths[digest] = path
                if path is None:
                    invalid = True
                    errors.append(f"不支持的共享 blob digest：{digest!r}")
                    continue
                try:
                    blob = ScannedFile.from_path(path)
                    blob_entries[digest] = blob
                    declared = _declared_size(descriptor)
                    if declared and blob.size != declared:
                        invalid = True
                        errors.append(f"共享 blob 大小不符：{path.name}")
                except OSError:
                    blob_entries[digest] = None
                    if path.exists():
                        invalid = True
                        errors.append(f"共享 blob 无法读取：{path.name}")
                    else:
                        missing = True
                        errors.append(f"共享 blob 缺失：{path.name}")

            config_path = blob_paths.get(config_descriptor.get("digest", ""))
            config: dict[str, Any] = {}
            if config_path is not None and blob_entries.get(config_descriptor.get("digest", "")) is not None:
                try:
                    config = _read_config(config_path)
                except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    invalid = True
                    errors.append(f"Ollama 配置元数据无效：{exc}")

            weight_layers = [
                layer for layer in layers
                if ".image.model" in str(layer.get("mediaType", ""))
            ]
            if not weight_layers:
                weight_layers = layers
            weight_digests = list(dict.fromkeys(
                layer.get("digest", "") for layer in weight_layers
                if isinstance(layer.get("digest", ""), str)
            ))
            shard_paths = [blob_paths.get(digest) for digest in weight_digests]
            model.shard_files = [str(path) for path in shard_paths if path is not None]
            model.shard_count = len(weight_digests)
            model.size_bytes = sum(
                blob_entries[digest].size for digest in weight_digests
                if blob_entries.get(digest) is not None
            )
            model.architecture = str(
                config.get("architecture", config.get("model_family", "")) or ""
            )[:256]
            model.params_b = _params_b(config.get("model_type"))
            context = config.get("context_length", 0)
            model.context_length = context if isinstance(context, int) and context > 0 else 0
            model.quant = str(config.get("file_type", "") or "")[:128]
            model.license = str(config.get("license", "") or "")[:1024]
            model.integrity = "invalid" if invalid else "incomplete" if missing else "valid"
            model.integrity_error = "；".join(dict.fromkeys(errors))
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            model.integrity = "invalid"
            model.integrity_error = f"Ollama manifest 解析失败：{exc}"
        except Exception as exc:
            model.integrity = "invalid"
            model.integrity_error = f"Ollama manifest 处理失败：{exc}"

        result.models.append(model)
        if model.integrity_error:
            result.issues.append((str(manifest.path), model.integrity_error))
    return result
