"""只读目录遍历：跳过系统目录、链接和重解析点，并隔离 IO 错误。"""
from __future__ import annotations

import ctypes
import os
import stat
import string
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..core.config import SKIP_DIR_NAMES
from ..core.errors import OperationCancelledError

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    size: int
    mtime: float
    mtime_ns: int

    @classmethod
    def from_path(cls, path: str | Path) -> "ScannedFile":
        target = Path(path)
        info = os.stat(target, follow_symlinks=False)
        if is_link_or_reparse(target, info):
            raise OSError("不跟随符号链接或重解析点")
        if not stat.S_ISREG(info.st_mode):
            raise OSError("不是普通文件")
        return cls(target, info.st_size, info.st_mtime, info.st_mtime_ns)


def is_link_or_reparse(path: str | Path, info: os.stat_result | None = None) -> bool:
    """同时识别 POSIX 符号链接和 Windows junction/reparse point。"""
    target = Path(path)
    if target.is_symlink():
        return True
    if info is None:
        info = os.stat(target, follow_symlinks=False)
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def check_cancelled(token: Any) -> None:
    if token is None:
        return
    raiser = getattr(token, "raise_if_cancelled", None)
    if callable(raiser):
        raiser()
        return
    cancelled = getattr(token, "cancelled", False)
    if callable(cancelled):
        cancelled = cancelled()
    if not cancelled:
        checker = getattr(token, "is_cancelled", None)
        cancelled = checker() if callable(checker) else False
    if cancelled:
        raise OperationCancelledError("扫描已取消")


def enumerate_drives() -> list[Path]:
    """返回当前可访问盘符；非 Windows 平台返回用户目录。"""
    if not sys.platform.startswith("win"):
        return [Path.home()]
    bitmask = 0
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        pass
    drives = []
    for letter in string.ascii_uppercase:
        if bitmask and not bitmask & (1 << (ord(letter) - ord("A"))):
            continue
        root = Path(f"{letter}:\\")
        if root.exists():
            drives.append(root)
    if not drives:
        fallback = Path(os.environ.get("SystemDrive", "C:") + "\\")
        if fallback.exists():
            drives.append(fallback)
    return drives


class DirectoryWalker:
    """迭代遍历目录。单个目录或文件失败不会中断整个扫描。"""

    def __init__(self, skip_dir_names: set[str] | None = None):
        names = SKIP_DIR_NAMES if skip_dir_names is None else skip_dir_names
        self.skip_dir_names = {name.casefold() for name in names}
        self.issues: list[tuple[str, str]] = []

    def walk(self, root: str | Path, token: Any = None) -> Iterator[ScannedFile]:
        self.issues = []
        start = Path(os.path.abspath(os.fspath(root)))
        stack = [start]
        visited: set[tuple[Any, ...]] = set()

        while stack:
            check_cancelled(token)
            directory = stack.pop()
            try:
                info = os.stat(directory, follow_symlinks=False)
                if is_link_or_reparse(directory, info):
                    self.issues.append((str(directory), "已跳过符号链接或重解析点目录"))
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    self.issues.append((str(directory), "扫描根不是目录"))
                    continue
                identity = self._directory_identity(directory, info)
                if identity in visited:
                    continue
                visited.add(identity)
            except PermissionError as exc:
                self.issues.append((str(directory), f"无权读取目录：{exc}"))
                continue
            except OSError as exc:
                self.issues.append((str(directory), f"无法读取目录：{exc}"))
                continue

            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        check_cancelled(token)
                        try:
                            entry_info = entry.stat(follow_symlinks=False)
                            if is_link_or_reparse(entry.path, entry_info):
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.casefold() not in self.skip_dir_names:
                                    stack.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            yield ScannedFile(
                                Path(entry.path), entry_info.st_size,
                                entry_info.st_mtime, entry_info.st_mtime_ns,
                            )
                        except PermissionError as exc:
                            self.issues.append((entry.path, f"无权读取文件：{exc}"))
                        except OSError as exc:
                            self.issues.append((entry.path, f"无法读取文件：{exc}"))
            except PermissionError as exc:
                self.issues.append((str(directory), f"无权枚举目录：{exc}"))
            except OSError as exc:
                self.issues.append((str(directory), f"无法枚举目录：{exc}"))

    @staticmethod
    def _directory_identity(path: Path, info: os.stat_result) -> tuple[Any, ...]:
        inode = getattr(info, "st_ino", 0)
        if inode:
            return ("inode", getattr(info, "st_dev", 0), inode)
        normalised = os.path.normcase(os.path.normpath(os.fspath(path)))
        return ("path", normalised)
