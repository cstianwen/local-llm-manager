"""显式根目录、安全文件工具及带不可伪造确认的编程改动工作区。"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import WorkspaceSecurityError
from .models import ChangeOperation
from .protocol import ChangeSuggestion, SuggestionParseResult, parse_change_suggestions


@dataclass(frozen=True)
class FileMatch:
    path: str
    size_bytes: int
    line: int = 0
    column: int = 0
    preview: str = ""


@dataclass
class SearchResult:
    matches: list[FileMatch] = field(default_factory=list)
    truncated: bool = False
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FileContent:
    path: str
    content: str
    size_bytes: int
    sha256: str


@dataclass
class PendingChange:
    workspace_root: str
    path: str
    operation: ChangeOperation
    change_set_id: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    original_sha256: str = ""
    proposed_sha256: str = ""
    original_size: int = 0
    diff: str = ""
    status: str = "pending"
    error: str = ""
    created_at: float = field(default_factory=time.time)
    original_content: str | None = field(default=None, repr=False)
    proposed_content: str | None = field(default=None, repr=False)

    def to_dict(self, include_content: bool = False) -> dict[str, Any]:
        result = asdict(self)
        result["operation"] = self.operation.value
        if not include_content:
            result.pop("original_content", None)
            result.pop("proposed_content", None)
        return result


@dataclass
class ChangeSet:
    workspace_root: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    status: str = "pending"
    changes: list[PendingChange] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_root": self.workspace_root,
            "created_at": self.created_at,
            "status": self.status,
            "changes": [item.to_dict() for item in self.changes],
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class ChangeConfirmation:
    change_id: str
    workspace_root: str
    path: str
    operation: str
    original_sha256: str
    proposed_sha256: str
    expires_at: float
    token: str = field(repr=False)


@dataclass(frozen=True)
class ApplyResult:
    change_id: str
    path: str
    success: bool
    status: str
    message: str
    sha256: str = ""


@dataclass
class _DiffLine:
    kind: str
    payload: str
    no_newline: bool = False


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[_DiffLine] = field(default_factory=list)


@dataclass
class _DiffFile:
    old_path: str
    new_path: str
    hunks: list[_Hunk] = field(default_factory=list)


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\r\n]*(?:\r\n|\n|\r|$)"
)


class CodingWorkspace:
    """文件写入只能经由 preview -> request_confirmation -> apply_change。"""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        max_file_bytes: int = 256 * 1024,
        max_search_results: int = 200,
        confirmation_ttl_seconds: float = 600.0,
    ):
        if max_file_bytes <= 0 or max_file_bytes > 4 * 1024 * 1024:
            raise ValueError("单文件限制必须在 1 字节到 4 MiB 之间")
        self.max_file_bytes = int(max_file_bytes)
        self.max_search_results = max(1, int(max_search_results))
        self.confirmation_ttl_seconds = max(1.0, float(confirmation_ttl_seconds))
        self._root: Path | None = None
        self._changes: dict[str, PendingChange] = {}
        self._sets: dict[str, ChangeSet] = {}
        self._confirmations: dict[str, ChangeConfirmation] = {}
        self._lock = threading.RLock()
        if root is not None:
            self.select_root(root)

    @property
    def root(self) -> str:
        return str(self._root) if self._root is not None else ""

    @staticmethod
    def _key(path: str | os.PathLike[str]) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))

    @classmethod
    def _inside(cls, path: Path, root: Path) -> bool:
        try:
            return os.path.commonpath((cls._key(path), cls._key(root))) == cls._key(root)
        except ValueError:
            return False

    @staticmethod
    def _system_directories() -> list[Path]:
        values = [
            os.environ.get("SystemRoot"), os.environ.get("WINDIR"),
            os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramData"),
        ]
        result = [Path(item) for item in values if item]
        if os.name != "nt":
            result.extend(Path(item) for item in ("/etc", "/bin", "/sbin", "/boot", "/proc", "/sys", "/dev"))
        return result

    def _check_sensitive(self, path: Path, *, selecting_root: bool = False) -> None:
        resolved = path.resolve(strict=False)
        if selecting_root and resolved.parent == resolved:
            raise WorkspaceSecurityError("不能把文件系统根目录选为编程工作区")
        if selecting_root and self._key(resolved) == self._key(Path.home().resolve()):
            raise WorkspaceSecurityError("不能把整个用户主目录选为编程工作区，请选择具体项目目录")
        for system_dir in self._system_directories():
            try:
                system = system_dir.resolve(strict=False)
            except OSError:
                continue
            if self._inside(resolved, system):
                raise WorkspaceSecurityError(f"拒绝访问敏感系统目录：{system}")
        blocked_parts = {".git", ".hg", ".svn", ".ssh", ".gnupg", ".aws", ".azure", ".kube"}
        if any(part.casefold() in blocked_parts for part in resolved.parts):
            raise WorkspaceSecurityError("拒绝访问版本库内部或凭据目录")
        name = resolved.name.casefold()
        blocked_names = {
            ".env", ".env.local", ".env.production", "credentials", "credentials.json",
            "secrets.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "ntuser.dat",
        }
        blocked_suffixes = {".pem", ".pfx", ".p12", ".kdbx", ".key"}
        if name in blocked_names or resolved.suffix.casefold() in blocked_suffixes:
            raise WorkspaceSecurityError(f"拒绝读取或修改可能的凭据文件：{resolved.name}")

    def select_root(self, root: str | os.PathLike[str]) -> str:
        if not os.fspath(root).strip():
            raise WorkspaceSecurityError("必须显式选择非空工作区根目录")
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceSecurityError("工作区根目录必须使用绝对路径")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceSecurityError(f"工作区不存在或无法解析：{candidate}（{exc}）") from exc
        if not resolved.is_dir():
            raise WorkspaceSecurityError("工作区根目录必须是已存在的目录")
        self._check_sensitive(resolved, selecting_root=True)
        with self._lock:
            self._root = resolved
        return str(resolved)

    def _require_root(self) -> Path:
        root = self._root
        if root is None:
            raise WorkspaceSecurityError("尚未显式选择编程工作区根目录")
        return root

    def resolve_path(self, relative_path: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
        root = self._require_root()
        value = os.fspath(relative_path)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise WorkspaceSecurityError("文件路径不能为空或包含 NUL")
        if os.path.isabs(value) or os.path.splitdrive(value)[0]:
            raise WorkspaceSecurityError("只允许工作区相对路径，拒绝绝对路径和盘符路径")
        relative = Path(value)
        if any(part == ".." for part in relative.parts):
            raise WorkspaceSecurityError("文件路径不能包含 ..")
        try:
            target = (root / relative).resolve(strict=False)
        except OSError as exc:
            raise WorkspaceSecurityError(f"无法解析文件路径：{value}（{exc}）") from exc
        if not self._inside(target, root) or self._key(target) == self._key(root):
            raise WorkspaceSecurityError("文件路径逃逸工作区，已拒绝")
        self._check_sensitive(target)
        if must_exist and not target.exists():
            raise FileNotFoundError(f"文件不存在：{value}")
        return target

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._require_root()).as_posix()

    def _read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes:
        limit = self.max_file_bytes if max_bytes is None else min(self.max_file_bytes, int(max_bytes))
        if not path.is_file():
            raise WorkspaceSecurityError(f"不是可读取的普通文件：{path}")
        size = path.stat().st_size
        if size > limit:
            raise WorkspaceSecurityError(f"文件 {path.name} 为 {size} 字节，超过 {limit} 字节限制")
        raw = path.read_bytes()
        if len(raw) > limit:
            raise WorkspaceSecurityError("文件读取期间大小超过限制")
        if b"\x00" in raw:
            raise WorkspaceSecurityError(f"检测到二进制 NUL 字节，拒绝按文本处理：{path.name}")
        return raw

    def read_text(self, relative_path: str | os.PathLike[str], *, max_bytes: int | None = None) -> FileContent:
        path = self.resolve_path(relative_path, must_exist=True)
        raw = self._read_bytes(path, max_bytes=max_bytes)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceSecurityError(f"仅支持 UTF-8 文本：{relative_path}（{exc}）") from exc
        return FileContent(
            path=self._relative(path), content=text, size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    read_file = read_text

    def search_files(
        self,
        query: str = "",
        *,
        content: bool = False,
        pattern: str = "*",
        max_results: int | None = None,
    ) -> SearchResult:
        """按相对路径/文件名检索，或显式搜索 UTF-8 文本内容。"""
        root = self._require_root()
        needle = query.casefold()
        if content and not needle:
            raise ValueError("内容检索需要非空 query")
        limit = min(self.max_search_results, max_results or self.max_search_results)
        result = SearchResult()
        visited = 0
        skipped_dirs = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv"}
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                name for name in dirs
                if name.casefold() not in skipped_dirs and not (Path(current) / name).is_symlink()
            ]
            for name in sorted(files):
                if len(result.matches) >= limit:
                    result.truncated = True
                    return result
                visited += 1
                if visited > 100_000:
                    result.truncated = True
                    result.issues.append("检索达到 100000 个文件的安全上限")
                    return result
                candidate = Path(current) / name
                try:
                    path = self.resolve_path(candidate.relative_to(root), must_exist=True)
                    relative = self._relative(path)
                    if not fnmatch.fnmatch(name, pattern) and not fnmatch.fnmatch(relative, pattern):
                        continue
                    size = path.stat().st_size
                    if not content:
                        if not needle or needle in relative.casefold():
                            result.matches.append(FileMatch(relative, size))
                        continue
                    if size > self.max_file_bytes:
                        continue
                    raw = self._read_bytes(path)
                    text = raw.decode("utf-8")
                    folded = text.casefold()
                    offset = folded.find(needle)
                    if offset < 0:
                        continue
                    line = folded.count("\n", 0, offset) + 1
                    line_start = folded.rfind("\n", 0, offset) + 1
                    column = offset - line_start + 1
                    preview = text.splitlines()[line - 1][:500] if text.splitlines() else ""
                    result.matches.append(FileMatch(relative, size, line, column, preview))
                except (OSError, UnicodeError, ValueError, WorkspaceSecurityError) as exc:
                    if content and not isinstance(exc, WorkspaceSecurityError):
                        result.issues.append(f"{name}: {exc}")
        return result

    def find_files(self, query: str = "", **kwargs: Any) -> SearchResult:
        kwargs["content"] = False
        return self.search_files(query, **kwargs)

    def search_content(self, query: str, **kwargs: Any) -> SearchResult:
        kwargs["content"] = True
        return self.search_files(query, **kwargs)

    @staticmethod
    def _header_path(line: str) -> str:
        value = line[4:].rstrip("\r\n").split("\t", 1)[0]
        if not value or value.startswith(('"', "'")) or value != value.strip():
            raise WorkspaceSecurityError("diff 文件路径为空、带引号或含歧义空白")
        return value

    @staticmethod
    def _clean_diff_path(value: str) -> str:
        if value == "/dev/null":
            return value
        if value.startswith(("a/", "b/")):
            value = value[2:]
        if not value or value.startswith(("/", "\\")) or os.path.isabs(value) or os.path.splitdrive(value)[0]:
            raise WorkspaceSecurityError(f"diff 含不安全路径：{value!r}")
        return value.replace("\\", "/")

    def _parse_diff(self, text: str) -> list[_DiffFile]:
        lines = text.splitlines(keepends=True)
        files: list[_DiffFile] = []
        index = 0
        while index < len(lines):
            if not lines[index].startswith("--- "):
                index += 1
                continue
            old_path = self._header_path(lines[index])
            index += 1
            if index >= len(lines) or not lines[index].startswith("+++ "):
                raise WorkspaceSecurityError("diff 的 --- 文件头后必须紧跟 +++ 文件头")
            new_path = self._header_path(lines[index])
            index += 1
            item = _DiffFile(self._clean_diff_path(old_path), self._clean_diff_path(new_path))
            while index < len(lines):
                if lines[index].startswith("--- "):
                    break
                if not lines[index].startswith("@@"):
                    if lines[index].strip() and not lines[index].startswith(
                        ("diff --git ", "index ", "new file mode ", "deleted file mode ")
                    ):
                        raise WorkspaceSecurityError(f"diff 在 hunk 外含无法识别的行：{lines[index].rstrip()!r}")
                    index += 1
                    continue
                match = _HUNK_HEADER.match(lines[index])
                if match is None:
                    raise WorkspaceSecurityError("diff hunk 头格式无效")
                hunk = _Hunk(
                    old_start=int(match.group(1)), old_count=int(match.group(2) or "1"),
                    new_start=int(match.group(3)), new_count=int(match.group(4) or "1"),
                )
                if hunk.old_start == 0 and hunk.old_count != 0:
                    raise WorkspaceSecurityError("diff old_start=0 时 old_count 必须为 0")
                if hunk.new_start == 0 and hunk.new_count != 0:
                    raise WorkspaceSecurityError("diff new_start=0 时 new_count 必须为 0")
                index += 1
                old_seen = new_seen = 0
                while index < len(lines):
                    line = lines[index]
                    if line.startswith("\\ No newline at end of file"):
                        if not hunk.lines or hunk.lines[-1].no_newline:
                            raise WorkspaceSecurityError("diff 的 no-newline 标记位置无效")
                        hunk.lines[-1].no_newline = True
                        index += 1
                        continue
                    if old_seen == hunk.old_count and new_seen == hunk.new_count:
                        break
                    if not line or line[0] not in {" ", "+", "-"}:
                        raise WorkspaceSecurityError("diff hunk 在声明行数完成前结束或含无效前缀")
                    kind = line[0]
                    hunk.lines.append(_DiffLine(kind, line[1:]))
                    old_seen += kind in {" ", "-"}
                    new_seen += kind in {" ", "+"}
                    if old_seen > hunk.old_count or new_seen > hunk.new_count:
                        raise WorkspaceSecurityError("diff hunk 实际行数超过头部声明")
                    index += 1
                if old_seen != hunk.old_count or new_seen != hunk.new_count:
                    raise WorkspaceSecurityError("diff hunk 实际行数与头部声明不符")
                item.hunks.append(hunk)
            if not item.hunks:
                raise WorkspaceSecurityError("仅支持包含实际文本 hunk 的 diff，不支持重命名或纯模式变更")
            files.append(item)
        if not files:
            raise WorkspaceSecurityError("未找到有效 unified diff 文件段")
        return files

    @staticmethod
    def _diff_line_value(line: _DiffLine) -> str:
        return line.payload.rstrip("\r\n") if line.no_newline else line.payload

    def _apply_diff_in_memory(self, item: _DiffFile, original: str) -> str:
        source = original.splitlines(keepends=True)
        output: list[str] = []
        cursor = 0
        for hunk in item.hunks:
            start = max(0, hunk.old_start - 1)
            if start < cursor or start > len(source):
                raise WorkspaceSecurityError("diff hunk 顺序或起始行无效")
            output.extend(source[cursor:start])
            cursor = start
            for line in hunk.lines:
                value = self._diff_line_value(line)
                if line.kind in {" ", "-"}:
                    if cursor >= len(source) or source[cursor] != value:
                        raise WorkspaceSecurityError(
                            f"diff 上下文与 {item.old_path} 原始内容不匹配，未生成可写入改动"
                        )
                    if line.kind == " ":
                        output.append(source[cursor])
                    cursor += 1
                else:
                    output.append(value)
        output.extend(source[cursor:])
        return "".join(output)

    @staticmethod
    def _render_diff(path: str, operation: ChangeOperation, old: str, new: str) -> str:
        old_name = "/dev/null" if operation == ChangeOperation.CREATE else f"a/{path}"
        new_name = "/dev/null" if operation == ChangeOperation.DELETE else f"b/{path}"
        pieces = difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=old_name, tofile=new_name, lineterm="\n",
        )
        rendered: list[str] = []
        for line in pieces:
            if line.endswith(("\n", "\r")):
                rendered.append(line)
            else:
                rendered.append(line + "\n")
                if line.startswith((" ", "+", "-")):
                    rendered.append("\\ No newline at end of file\n")
        return "".join(rendered)

    def _original(self, path: Path, operation: ChangeOperation) -> tuple[bytes, str]:
        if operation == ChangeOperation.CREATE:
            if os.path.lexists(path):
                raise WorkspaceSecurityError(f"新增目标已存在：{self._relative(path)}")
            return b"", ""
        if not os.path.lexists(path):
            raise WorkspaceSecurityError(f"修改/删除目标不存在：{self._relative(path)}")
        raw = self._read_bytes(path)
        try:
            return raw, raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceSecurityError("仅支持 UTF-8 文本改动") from exc

    def _make_pending(
        self,
        change_set_id: str,
        path: Path,
        operation: ChangeOperation,
        raw: bytes,
        original: str,
        proposed: str | None,
    ) -> PendingChange:
        relative = self._relative(path)
        if operation == ChangeOperation.DELETE:
            proposed = None
            comparison = ""
            proposed_bytes = b""
        else:
            if proposed is None:
                raise WorkspaceSecurityError("新增/修改必须提供完整新文本")
            proposed_bytes = proposed.encode("utf-8")
            if len(proposed_bytes) > self.max_file_bytes:
                raise WorkspaceSecurityError("拟写入内容超过单文件大小限制")
            comparison = proposed
            if proposed == original:
                raise WorkspaceSecurityError(f"文件内容没有变化：{relative}")
        diff = self._render_diff(relative, operation, original, comparison)
        if not diff:
            raise WorkspaceSecurityError(f"未生成有效差异：{relative}")
        return PendingChange(
            workspace_root=self.root, path=relative, operation=operation,
            change_set_id=change_set_id,
            original_sha256=hashlib.sha256(raw).hexdigest() if operation != ChangeOperation.CREATE else "",
            proposed_sha256=hashlib.sha256(proposed_bytes).hexdigest() if operation != ChangeOperation.DELETE else "",
            original_size=len(raw), diff=diff, original_content=original,
            proposed_content=proposed,
        )

    def _from_json_suggestion(self, set_id: str, suggestion: ChangeSuggestion) -> PendingChange:
        assert suggestion.operation is not None
        path = self.resolve_path(suggestion.path)
        raw, original = self._original(path, suggestion.operation)
        return self._make_pending(set_id, path, suggestion.operation, raw, original, suggestion.content)

    def _from_diff(self, set_id: str, diff: str) -> list[PendingChange]:
        pending: list[PendingChange] = []
        for item in self._parse_diff(diff):
            if item.old_path == "/dev/null" and item.new_path == "/dev/null":
                raise WorkspaceSecurityError("diff 不能同时从 /dev/null 到 /dev/null")
            operation = (
                ChangeOperation.CREATE if item.old_path == "/dev/null"
                else ChangeOperation.DELETE if item.new_path == "/dev/null"
                else ChangeOperation.MODIFY
            )
            path_value = item.new_path if operation != ChangeOperation.DELETE else item.old_path
            if operation == ChangeOperation.MODIFY and item.old_path != item.new_path:
                raise WorkspaceSecurityError("不支持通过 diff 重命名文件，请分别新增和删除")
            path = self.resolve_path(path_value)
            raw, original = self._original(path, operation)
            proposed = None if operation == ChangeOperation.DELETE else self._apply_diff_in_memory(item, original)
            pending.append(self._make_pending(set_id, path, operation, raw, original, proposed))
        return pending

    def preview_suggestions(self, suggestions: Iterable[ChangeSuggestion]) -> ChangeSet:
        root = self._require_root()
        change_set = ChangeSet(workspace_root=str(root))
        prepared: list[PendingChange] = []
        seen: set[str] = set()
        for suggestion in suggestions:
            if suggestion.unified_diff:
                items = self._from_diff(change_set.id, suggestion.unified_diff)
            elif suggestion.operation is not None:
                items = [self._from_json_suggestion(change_set.id, suggestion)]
            else:
                raise WorkspaceSecurityError("改动建议缺少 operation 或 unified_diff")
            for item in items:
                key = self._key(item.path)
                if key in seen:
                    raise WorkspaceSecurityError(f"同一文件出现重复改动：{item.path}")
                seen.add(key)
                prepared.append(item)
        if prepared:
            change_set.changes = prepared
            with self._lock:
                self._sets[change_set.id] = change_set
                for item in prepared:
                    self._changes[item.id] = item
        return change_set

    def preview_model_response(self, text: str) -> tuple[ChangeSet, SuggestionParseResult]:
        parsed = parse_change_suggestions(text)
        change_set = self.preview_suggestions(parsed.suggestions)
        change_set.issues.extend(parsed.issues)
        return change_set, parsed

    def preview_change(
        self, path: str, operation: ChangeOperation | str, content: str | None = None
    ) -> PendingChange:
        operation = ChangeOperation(operation)
        change_set = self.preview_suggestions([
            ChangeSuggestion(path=path, operation=operation, content=content)
        ])
        return change_set.changes[0]

    def get_change(self, change_id: str) -> PendingChange:
        try:
            return self._changes[change_id]
        except KeyError as exc:
            raise KeyError(f"未知 PendingChange：{change_id}") from exc

    def get_change_set(self, change_set_id: str) -> ChangeSet:
        try:
            return self._sets[change_set_id]
        except KeyError as exc:
            raise KeyError(f"未知 ChangeSet：{change_set_id}") from exc

    def request_confirmation(
        self, change_id: str, workspace_root: str | os.PathLike[str] | None = None
    ) -> ChangeConfirmation:
        change = self.get_change(change_id)
        if change.status != "pending":
            raise WorkspaceSecurityError(f"改动状态为 {change.status}，不能确认")
        root = self._require_root()
        supplied = self._key(workspace_root) if workspace_root is not None else self._key(root)
        if supplied != self._key(root) or supplied != self._key(change.workspace_root):
            raise WorkspaceSecurityError("确认的工作区与 PendingChange 不一致")
        confirmation = ChangeConfirmation(
            change_id=change.id, workspace_root=str(root), path=change.path,
            operation=change.operation.value, original_sha256=change.original_sha256,
            proposed_sha256=change.proposed_sha256,
            expires_at=time.time() + self.confirmation_ttl_seconds,
            token=secrets.token_urlsafe(32),
        )
        with self._lock:
            self._confirmations[change.id] = confirmation
        return confirmation

    def confirm_change_set(
        self, change_set_id: str, workspace_root: str | os.PathLike[str] | None = None
    ) -> list[ChangeConfirmation]:
        change_set = self.get_change_set(change_set_id)
        return [self.request_confirmation(item.id, workspace_root) for item in change_set.changes]

    def _fingerprint(self, path: Path) -> tuple[bool, int, str]:
        if not os.path.lexists(path):
            return False, 0, ""
        if not path.is_file():
            raise WorkspaceSecurityError("目标不再是普通文件")
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise WorkspaceSecurityError("目标文件已超过大小限制")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return True, size, digest.hexdigest()

    @staticmethod
    def _result(change: PendingChange, success: bool, status: str, message: str, sha256: str = "") -> ApplyResult:
        change.status = status
        change.error = "" if success else message
        return ApplyResult(change.id, change.path, success, status, message, sha256)

    def apply_change(
        self,
        change_id: str,
        confirmation: ChangeConfirmation,
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> ApplyResult:
        """验证一次性令牌、根目录和原始指纹后才执行新增/修改/删除。"""
        change = self.get_change(change_id)
        with self._lock:
            expected = self._confirmations.get(change_id)
            valid = (
                isinstance(confirmation, ChangeConfirmation)
                and expected is not None
                and confirmation.change_id == change_id
                and confirmation.path == expected.path == change.path
                and confirmation.operation == expected.operation == change.operation.value
                and confirmation.proposed_sha256 == expected.proposed_sha256 == change.proposed_sha256
                and secrets.compare_digest(confirmation.token, expected.token)
                and confirmation.expires_at == expected.expires_at
            )
            if valid:
                self._confirmations.pop(change_id, None)
        if not valid:
            return self._result(change, False, "blocked", "确认令牌无效、已使用或不存在；未写入")
        if time.time() > expected.expires_at:
            return self._result(change, False, "blocked", "确认令牌已过期；未写入")
        try:
            root = self._require_root()
            supplied = self._key(workspace_root) if workspace_root is not None else self._key(root)
            if (
                supplied != self._key(root)
                or supplied != self._key(change.workspace_root)
                or self._key(confirmation.workspace_root) != self._key(root)
            ):
                raise WorkspaceSecurityError("工作区在确认后发生变化")
            path = self.resolve_path(change.path)
            exists, size, digest = self._fingerprint(path)
            expected_exists = change.operation != ChangeOperation.CREATE
            if (
                exists != expected_exists
                or size != change.original_size
                or digest != change.original_sha256
                or confirmation.original_sha256 != change.original_sha256
            ):
                return self._result(
                    change, False, "conflict",
                    "原始文件指纹已变化（存在状态、大小或 SHA-256 不符）；未写入，请重新预览",
                )
            if change.operation == ChangeOperation.DELETE:
                path.unlink()
                if os.path.lexists(path):
                    raise OSError("删除调用后目标仍存在")
                result = self._result(change, True, "applied", f"已删除 {change.path}")
            else:
                assert change.proposed_content is not None
                path.parent.mkdir(parents=True, exist_ok=True)
                path = self.resolve_path(change.path)
                proposed_bytes = change.proposed_content.encode("utf-8")
                if hashlib.sha256(proposed_bytes).hexdigest() != expected.proposed_sha256:
                    raise WorkspaceSecurityError("拟写入内容在确认后发生变化")
                descriptor, temporary_name = tempfile.mkstemp(prefix=".local-model-agent-", dir=path.parent)
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(proposed_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if path.exists():
                        shutil.copymode(path, temporary)
                    os.replace(temporary, path)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual_sha != change.proposed_sha256:
                    raise OSError("写入后 SHA-256 校验失败")
                result = self._result(
                    change, True, "applied",
                    f"已{'新增' if change.operation == ChangeOperation.CREATE else '修改'} {change.path}",
                    actual_sha,
                )
            change_set = self._sets.get(change.change_set_id)
            if change_set and all(item.status in {"applied", "discarded"} for item in change_set.changes):
                change_set.status = "completed"
            return result
        except (OSError, ValueError, WorkspaceSecurityError) as exc:
            status = "conflict" if "指纹" in str(exc) else "blocked" if isinstance(exc, WorkspaceSecurityError) else "failed"
            return self._result(change, False, status, f"写入未执行或未成功：{exc}")

    def apply_change_set(
        self,
        change_set_id: str,
        confirmations: Iterable[ChangeConfirmation],
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> list[ApplyResult]:
        by_id = {item.change_id: item for item in confirmations}
        return [
            self.apply_change(
                item.id,
                by_id.get(item.id, ChangeConfirmation("", "", "", "", "", "", 0.0, "")),
                workspace_root,
            )
            for item in self.get_change_set(change_set_id).changes
        ]

    def discard_change_set(self, change_set_id: str) -> ChangeSet:
        change_set = self.get_change_set(change_set_id)
        with self._lock:
            for change in change_set.changes:
                if change.status != "applied":
                    change.status = "discarded"
                    self._confirmations.pop(change.id, None)
            change_set.status = "discarded"
        return change_set
