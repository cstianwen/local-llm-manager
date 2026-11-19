"""本地模型安全隔离、恢复、永久删除与受限磁盘瘦身。"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..core.config import app_data_dir, ollama_models_root
from ..core.errors import ModelManagerError
from ..core.models import ModelInfo, OperationResult
from ..providers.base import check_cancelled
from ..store.db import ModelStore

_INDEX_SCHEMA = "local-model-manager/quarantine"
_INDEX_VERSION = 1
_ENTRY_ID = re.compile(r"^[0-9a-f]{32}$")


def path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def inside_path(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    try:
        return os.path.commonpath((path_key(path), path_key(root))) == path_key(root)
    except (ValueError, OSError):
        return False


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(flag and attributes & flag)
    except OSError:
        return False


def _system_directories() -> list[Path]:
    values = [
        os.environ.get("SystemRoot"), os.environ.get("windir"),
        os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramData"),
    ]
    result = [Path(value) for value in values if value]
    if os.name != "nt":
        result.extend(Path(value) for value in ("/bin", "/boot", "/dev", "/etc", "/proc", "/sbin", "/sys", "/System"))
    return result


def validate_unprotected_file(path: Path, *, quarantine_root: Path | None = None) -> None:
    """拒绝根目录、系统目录、重解析点和本工具隔离区之外的伪装来源。"""
    if path.parent == path:
        raise ValueError(f"禁止操作根目录：{path}")
    if not path.is_file():
        raise ValueError(f"目标不存在或不是普通文件：{path}")
    if _is_reparse(path):
        raise ValueError(f"禁止隔离符号链接或重解析点：{path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"无法确认真实文件路径：{path}（{exc}）") from exc
    for protected in _system_directories():
        if inside_path(resolved, protected):
            raise ValueError(f"禁止操作系统目录中的文件：{resolved}")
    if quarantine_root is not None and inside_path(path, quarantine_root):
        raise ValueError("隔离区中的条目不能再次作为本地模型隔离")


def _ollama_shared_path(path: Path, model: ModelInfo) -> bool:
    if model.provider == "ollama" or model.format == "ollama" or model.file_path.startswith("ollama://"):
        return True
    roots = [ollama_models_root()]
    if model.source_dir:
        roots.append(Path(model.source_dir))
    candidates = [path]
    try:
        candidates.append(path.resolve(strict=False))
    except OSError:
        pass
    for candidate in candidates:
        for root in roots:
            if inside_path(candidate, root / "manifests") or inside_path(candidate, root / "blobs"):
                return True
        parts = [part.casefold() for part in candidate.parts]
        for index in range(len(parts) - 2):
            if parts[index:index + 2] == [".ollama", "models"] and parts[index + 2] in {"manifests", "blobs"}:
                return True
    return False


def _confirmation_required(action: str, data: dict[str, Any] | None = None) -> OperationResult:
    return OperationResult(
        success=False,
        message=f"{action}需要用户在界面中再次明确确认；本次未修改任何文件。",
        data=data or {},
        error="confirmation_required",
    )


class QuarantineService:
    """把登记的普通本地文件移入本工具隔离区；索引是唯一恢复依据。"""

    def __init__(self, store: ModelStore, root: str | os.PathLike[str] | None = None):
        self.store = store
        self.root = Path(root).expanduser().absolute() if root else app_data_dir() / "quarantine"
        if self.root.parent == self.root:
            raise ValueError("隔离区不能设为磁盘根目录")
        self.entries_root = self.root / "entries"
        self.index_path = self.root / "index.json"
        self.entries_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"schema": _INDEX_SCHEMA, "version": _INDEX_VERSION, "entries": []}
        try:
            with self.index_path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise ModelManagerError(f"隔离区索引无法读取：{exc}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != _INDEX_SCHEMA
            or payload.get("version") != _INDEX_VERSION
            or not isinstance(payload.get("entries"), list)
            or any(not isinstance(item, dict) or not _ENTRY_ID.fullmatch(str(item.get("id", ""))) for item in payload["entries"])
        ):
            raise ModelManagerError("隔离区索引格式或版本无效；未对任何文件执行操作")
        ids = [item["id"] for item in payload["entries"]]
        if len(ids) != len(set(ids)):
            raise ModelManagerError("隔离区索引含重复条目 ID；未对任何文件执行操作")
        return payload

    def _write_index(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".index-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.index_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._read_index()["entries"])

    list_entries = list

    def get(self, entry_id: str) -> dict[str, Any]:
        with self._lock:
            for entry in self._read_index()["entries"]:
                if entry["id"] == entry_id:
                    return copy.deepcopy(entry)
        raise KeyError(f"隔离区不存在条目：{entry_id}")

    def _model(self, model_id: str) -> ModelInfo:
        matches = [model for model in self.store.all() if model.model_id == model_id]
        if len(matches) != 1:
            raise KeyError(f"无法唯一确定数据库模型记录：{model_id}")
        return matches[0]

    def _entry_dir(self, entry_id: str) -> Path:
        if not _ENTRY_ID.fullmatch(entry_id):
            raise ValueError("隔离区条目 ID 无效")
        directory = self.entries_root / entry_id
        if not inside_path(directory, self.entries_root):
            raise ValueError("隔离区条目路径越界")
        return directory

    def _sources(self, model: ModelInfo) -> list[Path]:
        if model.provider != "local-file":
            raise ValueError("Ollama 等 Provider 模型必须走真实后端删除 API，不能伪装成本地隔离")
        if model.source_type not in {"", "file"}:
            raise ValueError("导入或后端登记不是经过本机扫描确认的文件记录，禁止据此移动文件")
        sources: list[Path] = []
        seen: set[str] = set()
        for value in [model.file_path, *model.shard_files]:
            path = Path(value).expanduser().absolute()
            key = path_key(path)
            if key in seen:
                continue
            seen.add(key)
            validate_unprotected_file(path, quarantine_root=self.root)
            if _ollama_shared_path(path, model):
                raise ValueError(f"禁止隔离 Ollama manifest 或共享 blob：{path}")
            sources.append(path)
        if not sources:
            raise ValueError("数据库记录没有可隔离的文件")
        return sources

    @staticmethod
    def _move_one(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".moving")
        if temporary.exists() or destination.exists():
            raise FileExistsError(f"隔离目标已存在：{destination}")
        shutil.move(str(source), str(temporary))
        os.replace(temporary, destination)

    @staticmethod
    def _rollback(moved: list[tuple[Path, Path]]) -> list[str]:
        errors: list[str] = []
        for source, destination in reversed(moved):
            try:
                if destination.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if os.path.lexists(source):
                        raise FileExistsError(f"原路径已出现其他文件：{source}")
                    shutil.move(str(destination), str(source))
            except OSError as exc:
                errors.append(f"恢复 {source} 失败：{exc}")
        return errors

    def quarantine_model(
        self,
        model_id: str,
        expected_path: str | os.PathLike[str] | None = None,
        *,
        confirmation: bool = False,
    ) -> OperationResult:
        model = self._model(model_id)
        if confirmation is not True:
            sources = self._sources(model)
            files = [
                {"path": str(source), "size_bytes": source.stat().st_size}
                for source in sources
            ]
            return _confirmation_required("移入隔离区", {
                "model_id": model.model_id,
                "expected_path": model.file_path,
                "files": files,
                "size_bytes": sum(item["size_bytes"] for item in files),
            })
        if expected_path is None:
            raise ValueError("必须显式传入与数据库记录一致的 expected_path")
        with self._lock:
            model = self._model(model_id)
            if path_key(expected_path) != path_key(model.file_path):
                raise ValueError("确认路径与数据库中的模型主路径不一致，已拒绝操作")
            sources = self._sources(model)
            entry_id = uuid.uuid4().hex
            entry_dir = self._entry_dir(entry_id)
            files_root = entry_dir / "files"
            if entry_dir.exists():
                raise FileExistsError("隔离条目 ID 冲突")
            mappings = []
            for index, source in enumerate(sources):
                relative = Path("files") / f"{index:04d}-{source.name}"
                mappings.append({
                    "original_path": str(source), "stored_path": relative.as_posix(),
                    "size_bytes": source.stat().st_size, "mtime": source.stat().st_mtime,
                })
            entry = {
                "id": entry_id, "created_at": time.time(), "model_id": model.model_id,
                "display_name": model.display_name or model.file_name,
                "original_path": model.file_path, "model": model.to_dict(), "files": mappings,
                "size_bytes": sum(item["size_bytes"] for item in mappings),
            }
            index = self._read_index()
            original_index = copy.deepcopy(index)
            moved: list[tuple[Path, Path]] = []
            index_changed = False
            delete_attempted = False
            try:
                for source, mapping in zip(sources, mappings):
                    destination = entry_dir / Path(mapping["stored_path"])
                    self._move_one(source, destination)
                    moved.append((source, destination))
                index["entries"].append(entry)
                self._write_index(index)
                index_changed = True
                delete_attempted = True
                self.store.delete(model.file_path)
            except BaseException as exc:
                rollback_errors = []
                if delete_attempted:
                    try:
                        self.store.upsert(model)
                    except Exception as db_exc:
                        rollback_errors.append(f"恢复数据库记录失败：{db_exc}")
                if index_changed:
                    try:
                        self._write_index(original_index)
                    except Exception as index_exc:
                        rollback_errors.append(f"恢复隔离索引失败：{index_exc}")
                rollback_errors.extend(self._rollback(moved))
                if rollback_errors:
                    raise ModelManagerError(f"隔离失败且回滚不完整：{exc}；{'；'.join(rollback_errors)}") from exc
                raise
            self.store.operations.add(
                "model.quarantine", model.model_id, message=f"已隔离 {len(moved)} 个文件；原路径已展示并经用户确认。",
                details={"entry_id": entry_id, "original_path": model.file_path, "size_bytes": entry["size_bytes"]},
            )
            return OperationResult(True, "模型文件已移入本工具隔离区，可恢复或再次确认后永久删除。", data={"entry": entry})

    def restore(
        self, entry_id: str, *, confirmation: bool = False, overwrite: bool = False
    ) -> OperationResult:
        if confirmation is not True:
            return _confirmation_required("恢复隔离条目", {"entry_id": entry_id})
        if overwrite:
            raise ValueError("为避免覆盖新文件，恢复操作不支持 overwrite=True；请先自行处理同名文件")
        with self._lock:
            index = self._read_index()
            entry = next((item for item in index["entries"] if item["id"] == entry_id), None)
            if entry is None:
                raise KeyError(f"隔离区不存在条目：{entry_id}")
            model = ModelInfo.from_dict(entry["model"])
            if model.source_type not in {"", "file"}:
                raise ModelManagerError("隔离索引中的模型来源无效")
            moves: list[tuple[Path, Path]] = []
            for mapping in entry["files"]:
                source = self._entry_dir(entry_id) / Path(mapping["stored_path"])
                target = Path(mapping["original_path"])
                if not inside_path(source, self._entry_dir(entry_id) / "files"):
                    raise ValueError("隔离索引含越界存储路径")
                if not source.is_file() or _is_reparse(source):
                    raise FileNotFoundError(f"隔离文件缺失或类型不安全：{source}")
                validate_unprotected_file(target, quarantine_root=self.root) if target.exists() else None
                if target.parent == target or any(inside_path(target, item) for item in _system_directories()):
                    raise ValueError(f"禁止恢复到系统目录或根目录：{target}")
                # 隔离后原父目录可能被替换为 junction；不能沿着重解析点写到其他位置。
                for ancestor in (target.parent, *target.parent.parents):
                    if ancestor.exists() and _is_reparse(ancestor):
                        raise ValueError(f"恢复路径的父目录含符号链接或重解析点：{ancestor}")
                if _ollama_shared_path(target, model):
                    raise ValueError("禁止把隔离文件恢复为 Ollama manifest/blob")
                if os.path.lexists(target):
                    raise FileExistsError(f"原路径已存在文件，未覆盖：{target}")
                moves.append((source, target))
            original_index = copy.deepcopy(index)
            completed: list[tuple[Path, Path]] = []
            index_changed = False
            upsert_attempted = False
            try:
                for source, target in moves:
                    self._move_one(source, target)
                    completed.append((target, source))
                index["entries"] = [item for item in index["entries"] if item["id"] != entry_id]
                self._write_index(index)
                index_changed = True
                upsert_attempted = True
                self.store.upsert(model)
            except BaseException as exc:
                rollback_errors = []
                if upsert_attempted:
                    try:
                        self.store.delete(model.file_path)
                    except Exception as db_exc:
                        rollback_errors.append(f"回滚数据库失败：{db_exc}")
                if index_changed:
                    try:
                        self._write_index(original_index)
                    except Exception as index_exc:
                        rollback_errors.append(f"回滚隔离索引失败：{index_exc}")
                rollback_errors.extend(self._rollback(completed))
                if rollback_errors:
                    raise ModelManagerError(f"恢复失败且回滚不完整：{exc}；{'；'.join(rollback_errors)}") from exc
                raise
            self._remove_empty_entry(entry_id)
            self.store.operations.add("quarantine.restore", entry_id, message="隔离条目已恢复到原路径。", details={"model_id": model.model_id})
            return OperationResult(True, "隔离条目已恢复到原路径，并重新写入模型库。", data={"model": model.to_dict()})

    def _remove_empty_entry(self, entry_id: str) -> None:
        directory = self._entry_dir(entry_id)
        for candidate in (directory / "files", directory):
            try:
                candidate.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ModelManagerError(f"隔离条目已处理，但空目录清理失败：{candidate}：{exc}") from exc

    def permanent_delete(
        self,
        entry_id: str,
        *,
        confirmation: bool = False,
        expected_path: str | os.PathLike[str] | None = None,
    ) -> OperationResult:
        if confirmation is not True:
            return _confirmation_required("永久删除隔离条目", {"entry_id": entry_id})
        with self._lock:
            index = self._read_index()
            entry = next((item for item in index["entries"] if item["id"] == entry_id), None)
            if entry is None:
                raise KeyError("永久删除只允许针对隔离区索引中的现有条目")
            if expected_path is not None and path_key(expected_path) != path_key(entry["original_path"]):
                raise ValueError("再次确认的原路径与隔离条目不匹配")
            directory = self._entry_dir(entry_id)
            if directory.exists() and _is_reparse(directory):
                raise ValueError("隔离条目目录是重解析点，拒绝删除")
            freed = 0
            if directory.exists():
                for root, dirnames, filenames in os.walk(directory, topdown=False, followlinks=False):
                    root_path = Path(root)
                    for name in filenames:
                        path = root_path / name
                        if not inside_path(path, directory):
                            raise ValueError("永久删除路径越出隔离条目")
                        if _is_reparse(path):
                            path.unlink()  # 只删除链接本身，不触碰链接目标。
                        else:
                            try:
                                freed += path.stat().st_size
                            except FileNotFoundError:
                                continue
                            path.unlink()
                    for name in dirnames:
                        path = root_path / name
                        if _is_reparse(path):
                            path.unlink()
                        else:
                            path.rmdir()
                directory.rmdir()
            index["entries"] = [item for item in index["entries"] if item["id"] != entry_id]
            self._write_index(index)
            self.store.operations.add(
                "quarantine.permanent_delete", entry_id,
                message="已永久删除隔离区内的条目；未触碰任何原路径或模型库其他文件。",
                details={"freed_bytes": freed, "original_path": entry["original_path"]},
            )
            return OperationResult(True, "隔离条目已永久删除，无法再通过本工具恢复。", data={"freed_bytes": freed})


class DiskMaintenanceService:
    """只清理明确位于本工具 cache 的文件及用户逐项确认的 quarantine 条目。"""

    def __init__(
        self,
        store: ModelStore,
        quarantine: QuarantineService | None = None,
        *,
        data_dir: str | os.PathLike[str] | None = None,
    ):
        base = Path(data_dir).expanduser().absolute() if data_dir else app_data_dir()
        self.cache_dir = base / "cache"
        self.quarantine = quarantine or QuarantineService(store, base / "quarantine")
        if not inside_path(self.quarantine.root, base):
            raise ValueError("磁盘瘦身的隔离区必须位于本工具数据目录内")
        self.store = store

    def plan(self, *, older_than_seconds: float = 0) -> dict[str, Any]:
        if older_than_seconds < 0:
            raise ValueError("缓存时间范围不能为负数")
        cutoff = time.time() - older_than_seconds
        files = []
        if self.cache_dir.exists():
            for root, dirnames, filenames in os.walk(self.cache_dir, followlinks=False):
                dirnames[:] = [name for name in dirnames if not _is_reparse(Path(root) / name)]
                for name in filenames:
                    path = Path(root) / name
                    if not inside_path(path, self.cache_dir) or _is_reparse(path):
                        continue
                    try:
                        info = path.stat()
                    except OSError:
                        continue
                    if info.st_mtime <= cutoff:
                        files.append({"path": str(path), "size_bytes": info.st_size, "mtime": info.st_mtime})
        entries = [{"id": item["id"], "original_path": item["original_path"], "size_bytes": item["size_bytes"]} for item in self.quarantine.list()]
        return {
            "cache_root": str(self.cache_dir), "cache_files": files,
            "cache_bytes": sum(item["size_bytes"] for item in files),
            "quarantine_entries": entries,
            "quarantine_bytes": sum(item["size_bytes"] for item in entries),
            "notice": "仅显示本工具缓存和隔离区；不会扫描或删除任何模型原路径。",
        }

    def clean(
        self,
        *,
        confirmation: bool = False,
        older_than_seconds: float = 0,
        quarantine_entry_ids: tuple[str, ...] | list[str] = (),
    ) -> OperationResult:
        if confirmation is not True:
            return _confirmation_required("磁盘瘦身", self.plan(older_than_seconds=older_than_seconds))
        plan = self.plan(older_than_seconds=older_than_seconds)
        freed = 0
        errors: list[str] = []
        for item in plan["cache_files"]:
            path = Path(item["path"])
            try:
                if not inside_path(path, self.cache_dir) or not path.is_file() or _is_reparse(path):
                    raise ValueError("路径已变化或不再符合缓存清理范围")
                path.unlink()
                freed += item["size_bytes"]
            except OSError as exc:
                errors.append(f"{path}：{exc}")
            except ValueError as exc:
                errors.append(str(exc))
        for entry_id in dict.fromkeys(quarantine_entry_ids):
            try:
                result = self.quarantine.permanent_delete(entry_id, confirmation=True)
                freed += int(result.data.get("freed_bytes", 0))
            except Exception as exc:
                errors.append(f"隔离条目 {entry_id}：{exc}")
        # 只移除已经为空的缓存子目录，绝不递归删除目录。
        if self.cache_dir.exists():
            for root, dirnames, _ in os.walk(self.cache_dir, topdown=False, followlinks=False):
                for name in dirnames:
                    try:
                        (Path(root) / name).rmdir()
                    except OSError:
                        pass
        self.store.operations.add(
            "disk.clean", str(self.cache_dir), status="success" if not errors else "partial",
            message=f"受限磁盘瘦身释放 {freed} 字节。" if not errors else "磁盘瘦身部分完成，错误已逐项返回。",
            details={"freed_bytes": freed, "errors": errors, "quarantine_entry_ids": list(quarantine_entry_ids)},
        )
        return OperationResult(
            not errors,
            "磁盘瘦身完成；未删除任何模型原路径。" if not errors else "磁盘瘦身部分完成，请查看失败原因。",
            data={"freed_bytes": freed, "errors": errors}, error="partial_failure" if errors else "",
        )

    def clean_task(
        self,
        token: Any = None,
        progress: Callable[[Any], None] | None = None,
        **options: Any,
    ) -> OperationResult:
        check_cancelled(token)
        if progress:
            progress({"phase": "plan"})
        result = self.clean(**options)
        check_cancelled(token)
        if progress:
            progress({
                "phase": "completed", "success": result.success,
                "freed_bytes": result.data.get("freed_bytes", 0),
            })
        return result


SafeDeleteService = QuarantineService
DiskSlimService = DiskMaintenanceService
