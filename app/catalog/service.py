"""官方模型库联网刷新、严格缓存校验与内置离线回退。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit

from ..core.config import app_data_dir
from ..core.errors import OperationCancelledError
from ..core.models import DictSerializable
from ..providers.base import check_cancelled
from ..store.db import ModelStore
from .resources import (
    BUILTIN_CATEGORIES,
    BUILTIN_RESOURCES,
    CatalogCategory,
    CatalogResource,
    builtin_categories,
    builtin_resources,
)

_CACHE_SCHEMA = "local-model-manager/official-catalog-cache"
_CACHE_VERSION = 1
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_MODELS_PER_SOURCE = 100
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
DEFAULT_REFRESH_SOURCES = (
    "https://huggingface.co/api/models?author=Qwen&limit=30",
)


@dataclass
class CatalogSnapshot(DictSerializable):
    resources: list[CatalogResource] = field(default_factory=list)
    source: str = "builtin"          # builtin / cache / refreshed
    refreshed_at: float = 0.0
    stale: bool = False
    errors: list[str] = field(default_factory=list)
    cache_path: str = ""


class CatalogService:
    """构造和列举不联网；只有显式 refresh() 才访问官方 HTTPS API。"""

    def __init__(
        self,
        *,
        store: ModelStore | None = None,
        cache_path: str | os.PathLike[str] | None = None,
        refresh_sources: Iterable[str] = DEFAULT_REFRESH_SOURCES,
        cache_ttl_seconds: float = 24 * 3600,
        timeout: float = 15.0,
        opener: Callable[..., Any] | None = None,
    ):
        if cache_ttl_seconds < 0 or timeout <= 0:
            raise ValueError("缓存有效期不能为负数，网络超时必须大于 0")
        self.store = store
        self.cache_path = Path(cache_path).expanduser().absolute() if cache_path else app_data_dir() / "cache" / "catalog.json"
        self.refresh_sources = tuple(refresh_sources)
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen
        self.last_errors: list[str] = []

    def categories(self) -> list[CatalogCategory]:
        return builtin_categories()

    list_categories = categories

    def _validate_source(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "https" or parsed.hostname != "huggingface.co"
            or parsed.path != "/api/models" or parsed.username or parsed.password or parsed.fragment
        ):
            raise ValueError("官方库刷新只允许 Hugging Face 的 HTTPS /api/models 地址")
        query = parse_qs(parsed.query)
        if "limit" in query:
            try:
                limit = int(query["limit"][-1])
            except (ValueError, TypeError) as exc:
                raise ValueError("官方库刷新 limit 参数无效") from exc
            if not 1 <= limit <= _MAX_MODELS_PER_SOURCE:
                raise ValueError(f"官方库刷新 limit 必须在 1 到 {_MAX_MODELS_PER_SOURCE} 之间")

    def _fetch(self, url: str, token: Any = None) -> list[CatalogResource]:
        self._validate_source(url)
        check_cancelled(token)
        request = urllib.request.Request(url, headers={
            "Accept": "application/json", "User-Agent": "LocalModelManager/1.0",
        })
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ConnectionError(f"官方库 HTTP {exc.code}：{exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ConnectionError(f"官方库访问失败：{reason}") from exc
        check_cancelled(token)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("官方库响应超过 8 MiB 安全限制")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"官方库返回了无效 UTF-8 JSON：{exc}") from exc
        if not isinstance(payload, list) or len(payload) > _MAX_MODELS_PER_SOURCE:
            raise ValueError("官方库响应必须是模型对象数组，且数量不超过安全限制")
        result: list[CatalogResource] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("官方库模型数组含非对象条目")
            model_id = str(item.get("id") or item.get("modelId") or "")
            if not _MODEL_ID.fullmatch(model_id):
                raise ValueError(f"官方库含无效模型 ID：{model_id!r}")
            official = f"https://huggingface.co/{model_id}"
            tags = tuple(str(tag)[:80] for tag in item.get("tags", [])[:12] if isinstance(tag, str)) if isinstance(item.get("tags", []), list) else ()
            result.append(CatalogResource(
                id="hf-" + hashlib.sha256(model_id.casefold().encode("utf-8")).hexdigest()[:24],
                display_name=model_id, category="models", kind="model",
                description="来自 Hugging Face 官方 API 的模型元数据；未固定权重文件、版本、大小或哈希，请打开官方页面核对。",
                delivery="official-page", official_url=official, source_url=official,
                license="见官方模型页", online_required=True, offline_capable=True,
                tags=tags,
            ))
        return result

    @staticmethod
    def _validate_cached_resource(resource: CatalogResource) -> None:
        for url in (resource.official_url, resource.source_url):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "huggingface.co" or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("缓存模型的官方地址无效")
        if (
            not resource.id.startswith("hf-") or resource.kind != "model"
            or resource.delivery != "official-page" or resource.download_url
            or resource.requires_install
        ):
            raise ValueError("缓存资源越权：联网缓存只允许官方页面型模型，不能扩张下载白名单")

    def _read_cache(self) -> dict[str, Any]:
        with self.cache_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if (
            not isinstance(payload, dict) or payload.get("schema") != _CACHE_SCHEMA
            or payload.get("version") != _CACHE_VERSION
            or not isinstance(payload.get("resources"), list)
            or not isinstance(payload.get("fetched_at"), (int, float))
        ):
            raise ValueError("缓存 schema/version 或字段无效")
        resources = []
        for item in payload["resources"]:
            if not isinstance(item, dict):
                raise ValueError("缓存资源不是对象")
            if isinstance(item.get("tags"), list):
                item = {**item, "tags": tuple(item["tags"])}
            resource = CatalogResource.from_dict(item)
            self._validate_cached_resource(resource)
            resources.append(resource)
        payload["resources"] = resources
        errors = payload.get("errors", [])
        payload["errors"] = [str(item) for item in errors] if isinstance(errors, list) else []
        return payload

    def _write_cache(self, resources: list[CatalogResource], errors: list[str], fetched_at: float) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": _CACHE_SCHEMA, "version": _CACHE_VERSION, "fetched_at": fetched_at,
            "resources": [item.to_dict() for item in resources], "errors": errors,
        }
        temporary = self.cache_path.with_name(f".{self.cache_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.cache_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _merge(dynamic: Iterable[CatalogResource]) -> list[CatalogResource]:
        merged = {resource.id: resource for resource in BUILTIN_RESOURCES}
        for resource in dynamic:
            if resource.id not in merged:
                merged[resource.id] = resource
        return sorted(merged.values(), key=lambda item: (item.category, item.kind, item.display_name.casefold()))

    def snapshot(self) -> CatalogSnapshot:
        try:
            cache = self._read_cache()
        except FileNotFoundError:
            self.last_errors = []
            return CatalogSnapshot(self._merge(()), "builtin", cache_path=str(self.cache_path))
        except (OSError, ValueError, TypeError, UnicodeDecodeError) as exc:
            self.last_errors = [f"官方库缓存不可用，已使用内置离线目录：{exc}"]
            return CatalogSnapshot(self._merge(()), "builtin", stale=True, errors=list(self.last_errors), cache_path=str(self.cache_path))
        errors = list(cache["errors"])
        stale = time.time() - cache["fetched_at"] > self.cache_ttl_seconds
        self.last_errors = errors
        return CatalogSnapshot(
            self._merge(cache["resources"]), "cache", cache["fetched_at"], stale,
            errors, str(self.cache_path),
        )

    def list_resources(
        self,
        *,
        category: str = "",
        kind: str = "",
        search: str = "",
        required: bool | None = None,
    ) -> list[CatalogResource]:
        text = search.casefold().strip()
        result = []
        for resource in self.snapshot().resources:
            if category and resource.category != category:
                continue
            if kind and resource.kind != kind:
                continue
            if required is not None and resource.required is not required:
                continue
            haystack = " ".join((resource.id, resource.display_name, resource.description, *resource.tags)).casefold()
            if text and text not in haystack:
                continue
            result.append(resource)
        return result

    resources = list_resources

    def get(self, resource_id: str) -> CatalogResource:
        for resource in self.list_resources():
            if resource.id == resource_id:
                return resource
        raise KeyError(f"资源目录中不存在：{resource_id}")

    get_resource = get

    def refresh(
        self,
        token: Any = None,
        progress: Callable[[Any], None] | None = None,
        *,
        sources: Iterable[str] | None = None,
        force: bool = False,
    ) -> CatalogSnapshot:
        """TaskManager 入口；网络失败也返回内置/缓存快照和逐项中文原因。"""
        check_cancelled(token)
        current = self.snapshot()
        if (
            not force and current.source == "cache" and current.refreshed_at
            and time.time() - current.refreshed_at <= self.cache_ttl_seconds
        ):
            return current
        selected = tuple(sources) if sources is not None else self.refresh_sources
        if not selected:
            error = "未配置官方库刷新地址；继续使用内置或缓存目录。"
            self.last_errors = [error]
            current.errors = [*current.errors, error]
            current.stale = True
            return current
        dynamic: dict[str, CatalogResource] = {}
        errors: list[str] = []
        succeeded = 0
        for index, url in enumerate(selected, 1):
            check_cancelled(token)
            if progress:
                progress({"phase": "fetch", "url": url, "index": index, "count": len(selected)})
            try:
                found = self._fetch(url, token)
                succeeded += 1
                for resource in found:
                    dynamic[resource.id] = resource
            except OperationCancelledError:
                raise
            except Exception as exc:
                errors.append(f"{url}：{exc}")
        if succeeded:
            fetched_at = time.time()
            try:
                self._write_cache(list(dynamic.values()), errors, fetched_at)
            except OSError as exc:
                errors.append(f"官方库缓存写入失败：{exc}")
            try:
                self._persist_resources(list(dynamic.values()), fetched_at)
            except Exception as exc:
                errors.append(f"官方库 SQLite 缓存写入失败：{exc}")
            if self.store:
                self.store.operations.add(
                    "catalog.refresh", status="success" if not errors else "partial",
                    message=f"官方库刷新得到 {len(dynamic)} 个模型条目。" if not errors else "官方库部分刷新失败，已保留可用目录。",
                    details={"sources": list(selected), "errors": errors, "model_count": len(dynamic)},
                )
            snapshot = CatalogSnapshot(
                self._merge(dynamic.values()), "refreshed", fetched_at,
                bool(errors), errors, str(self.cache_path),
            )
        else:
            # 完全失败时保留上一次完整缓存；没有缓存则使用内置清单。
            snapshot = current
            snapshot.errors = [*current.errors, *errors]
            snapshot.stale = True
            snapshot.source = "cache" if current.source == "cache" else "builtin"
        self.last_errors = snapshot.errors
        if progress:
            progress({
                "phase": "completed", "source": snapshot.source,
                "resource_count": len(snapshot.resources), "errors": snapshot.errors,
            })
        return snapshot

    refresh_official = refresh

    def _persist_resources(self, resources: list[CatalogResource], updated_at: float) -> None:
        if not self.store or not resources:
            return
        with self.store.transaction() as conn:
            conn.executemany(
                """INSERT INTO resources(
                       id, model_id, display_name, source, source_url,
                       license, size_bytes, metadata, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       display_name=excluded.display_name, source=excluded.source,
                       source_url=excluded.source_url, license=excluded.license,
                       metadata=excluded.metadata, updated_at=excluded.updated_at""",
                [(
                    item.id, item.model_ref, item.display_name, "Hugging Face 官方 API",
                    item.official_url, item.license, item.size_bytes,
                    json.dumps(item.to_dict(), ensure_ascii=False), updated_at,
                ) for item in resources],
            )


OfficialCatalogService = CatalogService
ResourceCatalog = CatalogService
