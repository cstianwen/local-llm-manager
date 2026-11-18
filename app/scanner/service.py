"""供 QRunnable/普通线程调用的本地模型扫描服务，不依赖 PySide6。"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.config import MIN_MODEL_SIZE_BYTES
from ..core.models import ModelInfo
from ..store.db import ModelStore
from .formats import (
    SHARD_PATTERN,
    ModelFormatError,
    display_name_from_path,
    inspect_file,
    is_index_file,
    is_model_candidate,
    model_format,
    quant_label,
    read_weight_map,
)
from .ollama import scan_ollama
from .walker import DirectoryWalker, ScannedFile, check_cancelled, enumerate_drives

_CACHE_SETTINGS_KEY = "scanner.parse_cache.v1"


@dataclass(frozen=True)
class ScanIssue:
    path: str
    message: str


@dataclass
class ScanProgress:
    phase: str = "pending"             # scanning / parsing / ollama / completed
    root: str = ""
    current_path: str = ""
    root_index: int = 0
    root_count: int = 0
    files_scanned: int = 0
    files_parsed: int = 0
    models_found: int = 0
    cache_hits: int = 0
    error_count: int = 0
    batch: list[ModelInfo] = field(default_factory=list)


@dataclass
class ScanResult:
    roots: list[str] = field(default_factory=list)
    models: list[ModelInfo] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)
    files_scanned: int = 0
    files_parsed: int = 0
    cache_hits: int = 0
    pruned: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0


class _ProgressReporter:
    def __init__(
        self,
        callback: Callable[[ScanProgress], None] | None,
        batch_size: int,
        file_interval: int,
    ):
        self.callback = callback
        self.batch_size = max(1, batch_size)
        self.file_interval = max(1, file_interval)
        self.state = ScanProgress()
        self.pending: list[ModelInfo] = []

    def emit(self, force: bool = False) -> None:
        if self.callback is None:
            self.pending.clear()
            return
        if force or self.pending:
            snapshot = replace(self.state, batch=list(self.pending))
            self.pending.clear()
            self.callback(snapshot)

    def start_root(self, root: str, index: int, count: int) -> None:
        self.state.phase = "scanning"
        self.state.root = root
        self.state.root_index = index
        self.state.root_count = count
        self.emit(force=True)

    def set_phase(self, phase: str, path: str = "") -> None:
        self.state.phase = phase
        if path:
            self.state.current_path = path
        self.emit(force=True)

    def file_scanned(self, path: Path) -> None:
        self.state.files_scanned += 1
        self.state.current_path = str(path)
        if self.state.files_scanned % self.file_interval == 0:
            self.emit(force=True)

    def file_parsed(self, path: Path, cache_hit: bool) -> None:
        self.state.files_parsed += 1
        self.state.current_path = str(path)
        if cache_hit:
            self.state.cache_hits += 1
        if self.state.files_parsed % self.file_interval == 0:
            self.emit(force=True)

    def model_found(self, model: ModelInfo) -> None:
        self.state.models_found += 1
        self.pending.append(model)
        if len(self.pending) >= self.batch_size:
            self.emit(force=True)

    def issue(self) -> None:
        self.state.error_count += 1


class _ParseCache:
    """复用 SettingsRepository 持久化 size+mtime_ns 指纹缓存，无需修改数据库 schema。"""

    def __init__(self, store: ModelStore, limit: int):
        self.store = store
        self.limit = max(100, limit)
        raw = store.settings.get(_CACHE_SETTINGS_KEY, {})
        self.data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        self.dirty = False
        while len(self.data) > self.limit:
            self.data.pop(next(iter(self.data)))
            self.dirty = True

    @staticmethod
    def key(path: str | Path) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))

    def get(self, entry: ScannedFile) -> dict[str, Any] | None:
        record = self.data.get(self.key(entry.path))
        if not isinstance(record, dict):
            return None
        if record.get("size") != entry.size or record.get("mtime_ns") != entry.mtime_ns:
            return None
        metadata = record.get("metadata")
        return dict(metadata) if isinstance(metadata, dict) else None

    def put(self, entry: ScannedFile, metadata: dict[str, Any]) -> None:
        key = self.key(entry.path)
        self.data.pop(key, None)
        self.data[key] = {
            "size": entry.size,
            "mtime_ns": entry.mtime_ns,
            "metadata": dict(metadata),
        }
        while len(self.data) > self.limit:
            self.data.pop(next(iter(self.data)))
        self.dirty = True

    def flush(self) -> None:
        if self.dirty:
            self.store.settings.set(_CACHE_SETTINGS_KEY, self.data)
            self.dirty = False


class ScannerService:
    """扫描入口可直接传给 ``TaskManager.submit``，也可在普通线程同步调用。"""

    def __init__(
        self,
        store: ModelStore,
        min_size_bytes: int = MIN_MODEL_SIZE_BYTES,
        progress_batch_size: int = 25,
        progress_file_interval: int = 250,
        cache_limit: int = 10_000,
    ):
        self.store = store
        self.min_size_bytes = max(0, int(min_size_bytes))
        self.progress_batch_size = max(1, int(progress_batch_size))
        self.progress_file_interval = max(1, int(progress_file_interval))
        self.cache_limit = max(100, int(cache_limit))

    @staticmethod
    def available_drives() -> list[Path]:
        return enumerate_drives()

    def scan_root(
        self,
        root: str | Path,
        token: Any = None,
        progress: Callable[[ScanProgress], None] | None = None,
        prune_missing: bool = True,
    ) -> ScanResult:
        return self.scan(
            token=token, progress=progress, roots=[root], include_ollama=False,
            prune_missing=prune_missing,
        )

    def scan(
        self,
        token: Any = None,
        progress: Callable[[ScanProgress], None] | None = None,
        roots: Iterable[str | Path] | None = None,
        include_ollama: bool = True,
        ollama_root: str | Path | None = None,
        prune_missing: bool = True,
    ) -> ScanResult:
        """ worker 前两个参数与 core.tasks.Task 对齐。

        示例：``tasks.submit("model-scan", service.scan, roots=[r"D:\\models"])``。
        """
        selected_roots = list(enumerate_drives() if roots is None else roots)
        root_paths: list[Path] = []
        seen_roots: set[str] = set()
        for root in selected_roots:
            path = Path(os.path.abspath(os.fspath(root)))
            key = _path_key(path)
            if key not in seen_roots:
                seen_roots.add(key)
                root_paths.append(path)

        reporter = _ProgressReporter(
            progress, self.progress_batch_size, self.progress_file_interval
        )
        result = ScanResult(
            roots=[str(path) for path in root_paths], started_at=time.time()
        )
        cache = _ParseCache(self.store, self.cache_limit)
        try:
            for index, root in enumerate(root_paths, 1):
                check_cancelled(token)
                reporter.start_root(str(root), index, len(root_paths))
                models, pruned, root_issues = self._scan_root(
                    root, token, reporter, cache, prune_missing
                )
                result.models.extend(models)
                result.issues.extend(root_issues)
                result.pruned += pruned

            if include_ollama:
                check_cancelled(token)
                reporter.set_phase("ollama")
                ollama_result = scan_ollama(ollama_root, token)
                for path, message in ollama_result.issues:
                    self._record_issue(result, reporter, path, message)
                self.store.upsert_many(ollama_result.models)
                if prune_missing and ollama_result.traversal_complete:
                    result.pruned += self._prune_ollama(ollama_result.models, ollama_result.models_root)
                for model in ollama_result.models:
                    reporter.model_found(model)
                result.models.extend(ollama_result.models)

            reporter.set_phase("completed")
            result.finished_at = time.time()
            result.files_scanned = reporter.state.files_scanned
            result.files_parsed = reporter.state.files_parsed
            result.cache_hits = reporter.state.cache_hits
            return result
        finally:
            # 取消时也保留已经完成解析的缓存，但不会在未完成 root 上 prune。
            cache.flush()

    def _scan_root(
        self,
        root: Path,
        token: Any,
        reporter: _ProgressReporter,
        cache: _ParseCache,
        prune_missing: bool,
    ) -> tuple[list[ModelInfo], int, list[ScanIssue]]:
        self.store.scan_roots.add(root)
        walker = DirectoryWalker()
        candidates: list[ScannedFile] = []
        for entry in walker.walk(root, token):
            reporter.file_scanned(entry.path)
            if is_model_candidate(entry, self.min_size_bytes):
                candidates.append(entry)

        reporter.set_phase("parsing", str(root))
        issues: list[ScanIssue] = []
        models = self._build_models(root, candidates, token, reporter, cache, issues)
        self.store.upsert_many(models)

        pruned = 0
        if prune_missing:
            if walker.issues:
                issues.append(ScanIssue(
                    str(root), "目录遍历不完整，为避免误清理，本次未删除失效登记"
                ))
            else:
                pruned = self.store.prune_missing(
                    {model.file_path for model in models}, root=root
                )
        self.store.scan_roots.touch(root)

        # walker 的错误在决定是否 prune 之后统一汇入结果。
        for path, message in walker.issues:
            issues.append(ScanIssue(path, message))
        for _ in issues:
            reporter.issue()
        return models, pruned, issues

    def _build_models(
        self,
        root: Path,
        candidates: list[ScannedFile],
        token: Any,
        reporter: _ProgressReporter,
        cache: _ParseCache,
        issues: list[ScanIssue],
    ) -> list[ModelInfo]:
        by_path = {_path_key(entry.path): entry for entry in candidates}
        consumed: set[str] = set()
        models: list[ModelInfo] = []
        indexes = sorted(
            (entry for entry in candidates if is_index_file(entry.path)),
            key=lambda entry: _path_key(entry.path),
        )

        for index_entry in indexes:
            check_cancelled(token)
            try:
                weight_map = read_weight_map(index_entry.path)
            except (OSError, ModelFormatError) as exc:
                metadata = _blank_metadata()
                metadata["integrity_error"] = str(exc)
                model = self._make_model(
                    root, index_entry.path, [index_entry], model_format(index_entry.path),
                    metadata, "invalid", str(exc), 0, [], 0,
                )
                issues.append(ScanIssue(str(index_entry.path), str(exc)))
                models.append(model)
                reporter.model_found(model)
                continue
            except Exception as exc:
                message = f"index JSON 处理失败：{exc}"
                metadata = _blank_metadata()
                model = self._make_model(
                    root, index_entry.path, [index_entry], model_format(index_entry.path),
                    metadata, "invalid", message, 0, [], 0,
                )
                issues.append(ScanIssue(str(index_entry.path), message))
                models.append(model)
                reporter.model_found(model)
                continue

            shard_entries: list[ScannedFile] = []
            shard_paths: list[Path] = []
            missing: list[Path] = []
            extra_errors: list[str] = []
            shard_names = list(dict.fromkeys(weight_map.values()))
            for shard_name in shard_names:
                try:
                    shard_path = _safe_shard_path(index_entry.path, shard_name)
                except ValueError as exc:
                    extra_errors.append(str(exc))
                    continue
                shard_paths.append(shard_path)
                key = _path_key(shard_path)
                entry = by_path.get(key)
                if entry is None:
                    try:
                        entry = ScannedFile.from_path(shard_path)
                    except OSError as exc:
                        if os.path.lexists(shard_path):
                            extra_errors.append(f"分片无法读取：{shard_path.name}（{exc}）")
                        else:
                            missing.append(shard_path)
                        continue
                consumed.add(key)
                shard_entries.append(entry)

            metadata_map = self._inspect_entries(
                shard_entries, token, reporter, cache, issues
            )
            metadatas = [metadata_map[_path_key(entry.path)] for entry in shard_entries]
            metadata = _merge_metadata(metadatas)
            integrity, error = _combined_integrity(metadatas, missing, extra_errors)
            if missing:
                issues.append(ScanIssue(str(index_entry.path), _missing_message(missing)))
            model = self._make_model(
                root, index_entry.path, shard_entries, model_format(index_entry.path),
                metadata, integrity, error, len(shard_names),
                [str(path) for path in shard_paths], 0, index_entry.mtime,
            )
            models.append(model)
            reporter.model_found(model)

        remaining = [
            entry for entry in candidates
            if not is_index_file(entry.path) and _path_key(entry.path) not in consumed
        ]
        grouped: dict[tuple[Any, ...], list[tuple[ScannedFile, Any]]] = {}
        singles: list[ScannedFile] = []
        for entry in remaining:
            match = SHARD_PATTERN.match(entry.path.stem)
            if not match:
                singles.append(entry)
                continue
            shard_index = int(match.group("index"))
            shard_count = int(match.group("count"))
            if not (1 <= shard_index <= shard_count <= 4096) or shard_count < 2:
                singles.append(entry)
                continue
            key = (
                _path_key(entry.path.parent), match.group("base").casefold(),
                shard_count, entry.path.suffix.casefold(),
            )
            grouped.setdefault(key, []).append((entry, match))

        for group in grouped.values():
            check_cancelled(token)
            first, first_match = min(
                group, key=lambda item: int(item[1].group("index"))
            )
            count = int(first_match.group("count"))
            width = len(first_match.group("index"))
            count_text = first_match.group("count")
            base = first_match.group("base")
            expected = [
                first.path.parent / (
                    f"{base}-{index:0{width}d}-of-{count_text}{first.path.suffix}"
                )
                for index in range(1, count + 1)
            ]
            actual_by_path = {_path_key(entry.path): entry for entry, _ in group}
            entries = [actual_by_path[_path_key(path)] for path in expected if _path_key(path) in actual_by_path]
            missing = [path for path in expected if _path_key(path) not in actual_by_path]
            metadata_map = self._inspect_entries(entries, token, reporter, cache, issues)
            metadatas = [metadata_map[_path_key(entry.path)] for entry in entries]
            metadata = _merge_metadata(metadatas)
            integrity, error = _combined_integrity(metadatas, missing, [])
            if missing:
                issues.append(ScanIssue(str(expected[0]), _missing_message(missing)))
            model = self._make_model(
                root, expected[0], entries, model_format(first.path), metadata,
                integrity, error, count, [str(path) for path in expected], 0,
            )
            models.append(model)
            reporter.model_found(model)

        grouped_paths = {
            _path_key(entry.path) for group in grouped.values() for entry, _ in group
        }
        for entry in singles:
            if _path_key(entry.path) in grouped_paths:
                continue
            check_cancelled(token)
            metadata_map = self._inspect_entries([entry], token, reporter, cache, issues)
            metadata = metadata_map[_path_key(entry.path)]
            model = self._make_model(
                root, entry.path, [entry], model_format(entry.path), metadata,
                metadata.get("integrity", "unknown"),
                str(metadata.get("integrity_error", "") or ""),
                1, [str(entry.path)], 1,
            )
            models.append(model)
            reporter.model_found(model)

        models.sort(key=lambda model: _path_key(model.file_path))
        return models

    def _inspect_entries(
        self,
        entries: list[ScannedFile],
        token: Any,
        reporter: _ProgressReporter,
        cache: _ParseCache,
        issues: list[ScanIssue],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            check_cancelled(token)
            key = _path_key(entry.path)
            if key in result:
                continue
            metadata = cache.get(entry)
            cache_hit = metadata is not None
            if metadata is None:
                try:
                    metadata = inspect_file(entry.path)
                except Exception as exc:
                    metadata = _blank_metadata()
                    metadata["integrity"] = "invalid"
                    metadata["integrity_error"] = f"模型文件处理失败：{exc}"
                cacheable = bool(metadata.pop("_cacheable", True))
                if cacheable:
                    cache.put(entry, metadata)
            else:
                metadata.pop("_cacheable", None)
            reporter.file_parsed(entry.path, cache_hit)
            result[key] = metadata
            if metadata.get("integrity_error"):
                issues.append(ScanIssue(
                    str(entry.path), str(metadata["integrity_error"])
                ))
        return result

    @staticmethod
    def _make_model(
        root: Path,
        canonical_path: Path,
        entries: list[ScannedFile],
        file_format: str,
        metadata: dict[str, Any],
        integrity: str,
        integrity_error: str,
        shard_count: int,
        shard_files: list[str],
        shard_index: int,
        extra_mtime: float = 0.0,
    ) -> ModelInfo:
        identity = _path_key(canonical_path)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        display_name = str(metadata.get("name", "") or "") or display_name_from_path(canonical_path)
        mtimes = [entry.mtime for entry in entries]
        if extra_mtime:
            mtimes.append(extra_mtime)
        return ModelInfo(
            file_path=str(canonical_path), file_name=canonical_path.name,
            format=file_format, size_bytes=sum(entry.size for entry in entries),
            architecture=str(metadata.get("architecture", "") or ""),
            params_b=float(metadata.get("params_b", 0.0) or 0.0),
            quant=quant_label(metadata), source_dir=str(root),
            mtime=max(mtimes, default=extra_mtime), provider="local-file",
            discovered_at=time.time(), model_id=f"local-{digest}",
            display_name=display_name,
            context_length=int(metadata.get("context_length", 0) or 0),
            license=str(metadata.get("license", "") or ""),
            integrity=integrity, integrity_error=integrity_error,
            source_type="file", shard_index=shard_index,
            shard_count=shard_count, shard_files=shard_files,
        )

    def _prune_ollama(self, models: list[ModelInfo], models_root: Path) -> int:
        """仅删除 SQLite 中失效的 manifest 登记，不触碰模型文件或共享 blob。"""
        manifests_root = models_root / "manifests"
        if not manifests_root.is_dir():
            return 0
        alive = {_path_key(model.file_path) for model in models}
        stale: list[str] = []
        for model in self.store.all():
            if model.provider != "ollama" or _path_key(model.source_dir) != _path_key(models_root):
                continue
            if not _inside(model.file_path, manifests_root):
                continue
            if _path_key(model.file_path) not in alive:
                stale.append(model.file_path)
        for path in stale:
            self.store.delete(path)
        return len(stale)

    @staticmethod
    def _record_issue(
        result: ScanResult,
        reporter: _ProgressReporter,
        path: str,
        message: str,
    ) -> None:
        result.issues.append(ScanIssue(path, message))
        reporter.issue()


# 兼容少量集成代码可能使用的名称，核心入口保持 ScannerService。
ModelScanner = ScannerService


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _inside(path: str | Path, root: str | Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root)
    except ValueError:
        return False


def _blank_metadata() -> dict[str, Any]:
    return {
        "architecture": "", "name": "", "params_b": 0.0,
        "context_length": 0, "quant": "", "quant_source": "",
        "license": "", "integrity": "invalid", "integrity_error": "",
    }


def _merge_metadata(metadatas: list[dict[str, Any]]) -> dict[str, Any]:
    merged = _blank_metadata()
    if not metadatas:
        merged["integrity"] = "unknown"
        return merged
    for field_name in ("architecture", "name", "license"):
        merged[field_name] = next(
            (str(item.get(field_name, "") or "") for item in metadatas if item.get(field_name)),
            "",
        )
    merged["params_b"] = round(sum(float(item.get("params_b", 0.0) or 0.0) for item in metadatas), 6)
    merged["context_length"] = max(
        (int(item.get("context_length", 0) or 0) for item in metadatas), default=0
    )
    quant_labels = list(dict.fromkeys(quant_label(item) for item in metadatas if item.get("quant")))
    merged["quant"] = "+".join(quant_labels)
    return merged


def _combined_integrity(
    metadatas: list[dict[str, Any]],
    missing: list[Path],
    extra_errors: list[str],
) -> tuple[str, str]:
    errors = list(extra_errors)
    errors.extend(
        str(item.get("integrity_error")) for item in metadatas if item.get("integrity_error")
    )
    if missing:
        errors.append(_missing_message(missing))
    invalid = bool(extra_errors) or any(item.get("integrity") == "invalid" for item in metadatas)
    if invalid:
        integrity = "invalid"
    elif missing:
        integrity = "incomplete"
    elif metadatas and all(item.get("integrity") == "valid" for item in metadatas):
        integrity = "valid"
    else:
        integrity = "unknown"
    return integrity, "；".join(dict.fromkeys(errors))


def _missing_message(missing: list[Path]) -> str:
    names = [path.name for path in missing[:5]]
    suffix = f" 等 {len(missing)} 个" if len(missing) > 5 else ""
    return f"模型不完整，缺少分片：{', '.join(names)}{suffix}"


def _safe_shard_path(index_path: Path, shard_name: str) -> Path:
    if "\x00" in shard_name or os.path.isabs(shard_name) or os.path.splitdrive(shard_name)[0]:
        raise ValueError(f"index 含不安全的分片路径：{shard_name!r}")
    base = Path(os.path.abspath(index_path.parent))
    candidate = Path(os.path.abspath(base / shard_name))
    if not _inside(candidate, base) or _path_key(candidate) == _path_key(index_path):
        raise ValueError(f"index 分片路径越界：{shard_name!r}")
    try:
        resolved_base = base.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"无法验证 index 分片路径：{shard_name!r}（{exc}）") from exc
    if not _inside(resolved_candidate, resolved_base):
        raise ValueError(f"index 分片不能通过链接指向目录外：{shard_name!r}")
    return candidate
