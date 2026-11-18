"""目录白名单 HTTPS 下载、Ollama 拉取、下载记录与仅计划的安装接口。"""
from __future__ import annotations

import hashlib
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ..core.config import app_data_dir
from ..core.errors import ModelManagerError, OperationCancelledError, UnsupportedOperationError
from ..core.models import DictSerializable, DownloadProgress, OperationResult
from ..providers.base import check_cancelled
from ..providers.registry import ProviderRegistry, create_default_registry
from ..store.db import ModelStore

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_JOB_FIELDS = {
    "status", "downloaded_bytes", "total_bytes", "speed_bps", "integrity",
    "integrity_error", "error", "started_at", "finished_at", "destination",
}


class _LocalCancellationToken:
    def __init__(self):
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise OperationCancelledError("任务已取消")


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        parsed = urlsplit(newurl)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise urllib.error.URLError("重定向目标不是无凭据 HTTPS 地址，已拒绝")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


@dataclass
class InstallPlan(DictSerializable):
    resource_id: str
    display_name: str
    downloaded_path: str
    official_url: str
    requires_confirmation: bool = True
    confirmed: bool = False
    execution_performed: bool = False
    action: str = "manual-install-after-user-confirmation"
    message: str = "请核对来源与文件后由用户确认并自行安装；下载服务不会执行下载内容。"


class DownloadService:
    """download_resource/download_https/pull_ollama 均可直接交给 TaskManager。"""

    def __init__(
        self,
        store: ModelStore,
        registry: ProviderRegistry | None = None,
        *,
        catalog: Any = None,
        download_dir: str | os.PathLike[str] | None = None,
        chunk_size: int = 128 * 1024,
        timeout: float = 30.0,
        opener: Any = None,
    ):
        if chunk_size <= 0 or timeout <= 0:
            raise ValueError("下载块大小和超时必须大于 0")
        self.store = store
        self.registry = registry or create_default_registry()
        self._owns_registry = registry is None
        if catalog is None:
            from ..catalog.service import CatalogService
            catalog = CatalogService(store=store)
        self.catalog = catalog
        self.download_dir = Path(download_dir).expanduser().absolute() if download_dir else app_data_dir() / "downloads"
        self.chunk_size = int(chunk_size)
        self.timeout = float(timeout)
        if opener is None:
            self._opener = urllib.request.build_opener(_HTTPSRedirectHandler()).open
        elif callable(opener):
            self._opener = opener
        elif callable(getattr(opener, "open", None)):
            self._opener = opener.open
        else:
            raise ValueError("opener 必须可调用或提供 open()")
        self._tokens: dict[int, Any] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _field(resource: Any, name: str, default: Any = None) -> Any:
        return resource.get(name, default) if isinstance(resource, dict) else getattr(resource, name, default)

    def _resource(self, resource_id: str) -> Any:
        getter = getattr(self.catalog, "get", None) or getattr(self.catalog, "get_resource", None)
        if not callable(getter):
            raise ValueError("资源目录缺少 get(resource_id) 接口")
        return getter(resource_id)

    def _resources(self) -> list[Any]:
        lister = getattr(self.catalog, "list_resources", None) or getattr(self.catalog, "resources", None)
        if not callable(lister):
            raise ValueError("资源目录缺少 list_resources() 接口")
        return list(lister())

    def _declared_resource(self, url: str, resource_id: str = "") -> Any:
        if resource_id:
            resource = self._resource(resource_id)
            declared = str(self._field(resource, "download_url", "") or "")
            if declared != url:
                raise ValueError("下载 URL 与该资源目录条目中声明的地址不一致")
        else:
            matches = [item for item in self._resources() if str(self._field(item, "download_url", "") or "") == url]
            if len(matches) != 1:
                raise ValueError("拒绝下载：URL 没有唯一匹配的内置/已缓存官方目录条目")
            resource = matches[0]
        self._validate_https(url)
        if str(self._field(resource, "delivery", "")) != "https-file":
            raise UnsupportedOperationError("该目录条目不是直接 HTTPS 文件；官方网页不会被伪装成安装包下载")
        return resource

    @staticmethod
    def _validate_https(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("只允许目录中声明的 HTTPS 官方 URL，HTTP/其他协议已拒绝")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("下载 URL 不得包含凭据或片段")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("下载 URL 端口无效") from exc

    def _create_job(
        self,
        *,
        resource_id: str,
        model_id: str = "",
        url: str,
        destination: str,
    ) -> int:
        now = time.time()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO download_jobs(
                       resource_id, model_id, url, destination, status,
                       created_at, started_at
                   ) VALUES (?, ?, ?, ?, 'downloading', ?, ?)""",
                (resource_id, model_id, url, destination, now, now),
            )
            return int(cursor.lastrowid)

    def _update_job(self, job_id: int, **changes: Any) -> None:
        unknown = set(changes) - _JOB_FIELDS
        if unknown:
            raise ValueError(f"未知下载记录字段：{', '.join(sorted(unknown))}")
        if not changes:
            return
        assignments = ", ".join(f"{key}=?" for key in changes)
        with self.store.transaction() as conn:
            conn.execute(f"UPDATE download_jobs SET {assignments} WHERE id=?", (*changes.values(), job_id))

    def _register(self, job_id: int, token: Any) -> Any:
        actual = token or _LocalCancellationToken()
        with self._lock:
            self._tokens[job_id] = actual
        return actual

    def _unregister(self, job_id: int) -> None:
        with self._lock:
            self._tokens.pop(job_id, None)

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            token = self._tokens.get(int(job_id))
        if token is None or not callable(getattr(token, "cancel", None)):
            return False
        token.cancel()
        return True

    def download_resource(
        self,
        token: Any = None,
        progress: Callable[[DownloadProgress], None] | None = None,
        resource_id: str = "",
        *,
        destination: str | os.PathLike[str] | None = None,
        overwrite: bool = False,
    ) -> OperationResult:
        resource = self._resource(resource_id)
        delivery = str(self._field(resource, "delivery", ""))
        if delivery == "ollama":
            model_ref = str(self._field(resource, "model_ref", "") or "")
            return self.pull_ollama(token, progress, model_ref, resource_id=resource_id)
        if delivery == "builtin":
            raise UnsupportedOperationError("该 Agent 已内置，无需下载；不会生成虚假下载任务")
        if delivery == "official-page":
            url = str(self._field(resource, "official_url", "") or self._field(resource, "source_url", ""))
            raise UnsupportedOperationError(f"该资源只提供官方页面，未把网页当作文件下载。请由用户打开：{url}")
        if delivery != "https-file":
            raise UnsupportedOperationError(f"不支持的资源交付方式：{delivery}")
        target = Path(destination).expanduser().absolute() if destination else self.download_dir / str(self._field(resource, "filename", "") or f"{resource_id}.download")
        return self.download_https(
            token, progress, str(self._field(resource, "download_url", "")), target,
            resource_id=resource_id, overwrite=overwrite,
        )

    def download_https(
        self,
        token: Any = None,
        progress: Callable[[DownloadProgress], None] | None = None,
        url: str = "",
        destination: str | os.PathLike[str] = "",
        *,
        resource_id: str = "",
        overwrite: bool = False,
    ) -> OperationResult:
        resource = self._declared_resource(url, resource_id)
        target = Path(destination).expanduser().absolute()
        if target.parent == target or (target.exists() and target.is_dir()):
            raise ValueError(f"下载目标必须是文件路径且不能为根目录：{target}")
        if target.exists() and not overwrite:
            raise FileExistsError(f"下载目标已存在，未覆盖：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        model_ref = str(self._field(resource, "model_ref", "") or "")
        job_id = self._create_job(
            resource_id=str(self._field(resource, "id", "") or resource_id),
            model_id=model_ref, url=url, destination=str(target),
        )
        token = self._register(job_id, token)
        part = target.with_name(f".{target.name}.{job_id}.part")
        started = time.monotonic()
        last_persist = 0.0
        downloaded = 0
        total = 0
        digest = hashlib.sha256()
        progress_id = model_ref or str(self._field(resource, "id", "") or resource_id)
        try:
            check_cancelled(token)
            request = urllib.request.Request(url, headers={
                "Accept": "application/octet-stream", "User-Agent": "LocalModelManager/1.0",
            }, method="GET")
            response = self._opener(request, timeout=self.timeout)
            with response:
                final_url = str(getattr(response, "url", "") or response.geturl() or url)
                self._validate_https(final_url)
                content_type = str(response.headers.get("Content-Type", "") or "").casefold()
                if "text/html" in content_type:
                    raise ModelManagerError("服务器返回 HTML 页面而非文件；未把官方网页伪装成安装包")
                raw_total = response.headers.get("Content-Length")
                try:
                    total = int(raw_total) if raw_total is not None else 0
                except (TypeError, ValueError):
                    total = 0
                expected_size = int(self._field(resource, "size_bytes", 0) or 0)
                if expected_size and total and expected_size != total:
                    raise ModelManagerError(f"目录声明大小 {expected_size} 与服务器大小 {total} 不一致")
                if expected_size:
                    total = expected_size
                expected_sha = str(self._field(resource, "sha256", "") or "")
                if expected_sha and not _SHA256.fullmatch(expected_sha):
                    raise ValueError("目录中的 SHA-256 格式无效，已停止下载")
                while True:
                    check_cancelled(token)
                    chunk = response.read(self.chunk_size)
                    check_cancelled(token)
                    if not chunk:
                        break
                    part.parent.mkdir(parents=True, exist_ok=True)
                    with part.open("ab") as stream:
                        stream.write(chunk)
                    downloaded += len(chunk)
                    digest.update(chunk)
                    if total and downloaded > total:
                        raise ModelManagerError("实际下载字节超过声明总大小，已停止且未完成目标文件")
                    elapsed = max(1e-9, time.monotonic() - started)
                    snapshot = DownloadProgress(
                        model_id=progress_id, downloaded_bytes=downloaded, total_bytes=total,
                        speed_bps=downloaded / elapsed, status="downloading",
                    )
                    if progress:
                        progress(snapshot)
                    now = time.monotonic()
                    if now - last_persist >= 0.5:
                        self._update_job(
                            job_id, downloaded_bytes=downloaded, total_bytes=total,
                            speed_bps=snapshot.speed_bps, status="downloading",
                        )
                        last_persist = now
                if total and downloaded != total:
                    raise ModelManagerError(f"下载不完整：声明 {total} 字节，实际收到 {downloaded} 字节")
                if downloaded == 0:
                    raise ModelManagerError("服务器未返回任何文件字节，未完成下载")
                actual_sha = digest.hexdigest()
                if expected_sha and actual_sha.casefold() != expected_sha.casefold():
                    self._update_job(job_id, integrity="invalid", integrity_error="SHA-256 与目录声明不匹配")
                    raise ModelManagerError("SHA-256 校验失败；临时 .part 保留，目标文件未生成")
                with part.open("ab") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                if part.stat().st_size != downloaded:
                    raise ModelManagerError("临时文件大小与接收字节不一致，未执行原子完成")
                if target.exists() and not overwrite:
                    raise FileExistsError(f"下载期间目标已出现，未覆盖：{target}")
                os.replace(part, target)
                integrity = "sha256-verified" if expected_sha else ("size-verified" if total else "digest-computed")
                elapsed = max(1e-9, time.monotonic() - started)
                snapshot = DownloadProgress(
                    model_id=progress_id, downloaded_bytes=downloaded, total_bytes=total,
                    speed_bps=downloaded / elapsed, status="completed",
                )
                self._update_job(
                    job_id, status="completed", downloaded_bytes=downloaded, total_bytes=total,
                    speed_bps=snapshot.speed_bps, integrity=integrity, integrity_error="",
                    error="", finished_at=time.time(),
                )
                if progress:
                    progress(snapshot)
                self.store.operations.add(
                    "download.completed", progress_id,
                    message="HTTPS 文件已流式下载并通过完成校验。",
                    details={"job_id": job_id, "path": str(target), "bytes": downloaded, "integrity": integrity},
                )
                return OperationResult(
                    True, "下载已完成并原子写入目标文件；未执行下载内容。",
                    data={
                        "job_id": job_id, "path": str(target), "bytes": downloaded,
                        "sha256": actual_sha, "integrity": integrity,
                    },
                )
        except OperationCancelledError as exc:
            message = "下载已取消；目标文件未生成，临时 .part 保留供清理。"
            self._update_job(
                job_id, status="cancelled", downloaded_bytes=downloaded, total_bytes=total,
                speed_bps=0, error=message, finished_at=time.time(),
            )
            self._emit_error(progress, progress_id, downloaded, total, "cancelled", message)
            raise
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                reason = f"HTTP {exc.code}：{exc.reason}"
            elif isinstance(exc, urllib.error.URLError):
                reason = str(exc.reason)
            else:
                reason = str(exc) or exc.__class__.__name__
            message = f"下载失败：{reason}；目标文件未完成。"
            try:
                self._update_job(
                    job_id, status="failed", downloaded_bytes=downloaded, total_bytes=total,
                    speed_bps=0, error=message, finished_at=time.time(),
                )
                self.store.operations.add("download.failed", progress_id, status="error", message=message, details={"job_id": job_id})
            except Exception:
                pass
            self._emit_error(progress, progress_id, downloaded, total, "failed", message)
            if isinstance(exc, (ModelManagerError, ValueError, OSError)):
                raise
            raise ModelManagerError(message) from exc
        finally:
            self._unregister(job_id)

    @staticmethod
    def _emit_error(
        progress: Callable[[DownloadProgress], None] | None,
        model_id: str,
        downloaded: int,
        total: int,
        status: str,
        message: str,
    ) -> None:
        if progress:
            progress(DownloadProgress(
                model_id=model_id, downloaded_bytes=downloaded, total_bytes=total,
                speed_bps=0, status=status, error=message,
            ))

    def pull_ollama(
        self,
        token: Any = None,
        progress: Callable[[DownloadProgress], None] | None = None,
        model_ref: str = "",
        *,
        resource_id: str = "",
    ) -> OperationResult:
        if not isinstance(model_ref, str) or not model_ref.strip():
            raise ValueError("Ollama model_ref 不能为空")
        provider = self.registry.get("ollama")
        job_id = self._create_job(
            resource_id=resource_id, model_id=model_ref,
            url=f"ollama://{model_ref}", destination="",
        )
        token = self._register(job_id, token)
        downloaded = 0
        total = 0
        last_persist = 0.0
        try:
            for event in provider.pull_model(model_ref, cancel_token=token):
                check_cancelled(token)
                downloaded, total = event.downloaded_bytes, event.total_bytes
                if progress:
                    progress(event)
                now = time.monotonic()
                if now - last_persist >= 0.5 or event.status == "completed":
                    self._update_job(
                        job_id, status="downloading", downloaded_bytes=downloaded,
                        total_bytes=total, speed_bps=event.speed_bps,
                    )
                    last_persist = now
            self._update_job(
                job_id, status="completed", downloaded_bytes=downloaded, total_bytes=total,
                speed_bps=0, integrity="provider-confirmed", error="", finished_at=time.time(),
            )
            base_url = str(getattr(provider, "base_url", "http://127.0.0.1:11434"))
            self.store.operations.add(
                "ollama.pull", model_ref, message="本机 Ollama 已确认模型拉取成功。",
                details={"job_id": job_id, "local_api": base_url, "external_repository_required": True},
            )
            return OperationResult(
                True, "本机 Ollama 已确认拉取成功；本工具调用的是本机 API，模型仓库联网由 Ollama 负责。",
                data={
                    "job_id": job_id, "model_ref": model_ref, "local_api": base_url,
                    "external_repository_required": True, "integrity": "provider-confirmed",
                },
            )
        except OperationCancelledError:
            message = "Ollama 拉取已取消；未显示为成功。"
            self._update_job(job_id, status="cancelled", downloaded_bytes=downloaded, total_bytes=total, speed_bps=0, error=message, finished_at=time.time())
            self._emit_error(progress, model_ref, downloaded, total, "cancelled", message)
            raise
        except Exception as exc:
            message = f"Ollama 拉取失败：{exc}。本机 API 与外网模型仓库是两项不同依赖，请分别检查。"
            self._update_job(job_id, status="failed", downloaded_bytes=downloaded, total_bytes=total, speed_bps=0, error=message, finished_at=time.time())
            self._emit_error(progress, model_ref, downloaded, total, "failed", message)
            raise
        finally:
            self._unregister(job_id)

    def installation_plan(
        self,
        resource_id: str,
        downloaded_path: str | os.PathLike[str] = "",
        *,
        confirmation: bool = False,
    ) -> InstallPlan:
        resource = self._resource(resource_id)
        if str(self._field(resource, "kind", "")) != "software":
            raise ValueError("安装计划只适用于软件资源")
        if not bool(self._field(resource, "requires_install", False)):
            raise ValueError("该资源不需要安装，不能生成安装计划")
        path = Path(downloaded_path).expanduser().absolute() if downloaded_path else Path()
        if downloaded_path and (not path.is_file() or path.suffix == ".part"):
            raise ValueError("安装计划需要已完成且非 .part 的真实下载文件")
        official = str(self._field(resource, "official_url", "") or self._field(resource, "source_url", "") or self._field(resource, "download_url", ""))
        return InstallPlan(
            resource_id=resource_id,
            display_name=str(self._field(resource, "display_name", "") or resource_id),
            downloaded_path=str(path) if downloaded_path else "", official_url=official,
            confirmed=confirmation is True,
            message=(
                "用户已确认此安装计划；下载服务仍不会执行文件，请由用户通过系统界面自行安装。"
                if confirmation is True else
                "安装前必须由用户核对官方来源、真实路径与影响并明确确认；本服务只返回计划，绝不执行下载内容。"
            ),
        )

    create_install_plan = installation_plan
    install_software = installation_plan

    def list_downloads(self, *, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("下载记录数量限制必须大于 0")
        sql = "SELECT * FROM download_jobs"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(row) for row in self.store.connection.execute(sql, params).fetchall()]

    def get_download(self, job_id: int) -> dict[str, Any]:
        row = self.store.connection.execute("SELECT * FROM download_jobs WHERE id=?", (int(job_id),)).fetchone()
        if row is None:
            raise KeyError(f"下载记录不存在：{job_id}")
        return dict(row)

    def close(self) -> None:
        if self._owns_registry:
            self.registry.close()


ModelDownloadService = DownloadService
Downloader = DownloadService
