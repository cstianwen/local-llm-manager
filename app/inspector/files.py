"""Windows Restart Manager 文件占用反查；不根据猜测指定占用方。"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from ..core.models import AppUser
from .base import Confidence, InspectionResult, resolve_process

ERROR_MORE_DATA = 234
ERROR_ACCESS_DENIED = 5


def _windows_error(code: int) -> str:
    try:
        return ctypes.FormatError(code).strip()
    except Exception:
        return f"Windows 错误码 {code}"


class RestartManagerInspector:
    """直接调用 rstrtmgr.dll，返回系统报告的精确文件占用进程。"""

    def inspect(self, path: str | os.PathLike[str]) -> InspectionResult:
        target = Path(path).expanduser().resolve()
        result = InspectionResult(target=str(target))
        if not target.is_file():
            result.errors.append(f"文件不存在或不是普通文件：{target}")
            result.message = "只能检查真实存在的普通文件。"
            return result
        if not sys.platform.startswith("win"):
            result.errors.append("Restart Manager 仅支持 Windows；当前平台未伪造占用结果。")
            result.message = "当前平台文件占用方未知。"
            return result

        session = wintypes.DWORD()
        session_key = ctypes.create_unicode_buffer(33)
        process_infos = []
        try:
            manager = ctypes.windll.LoadLibrary("rstrtmgr.dll")
            start = manager.RmStartSession
            start.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
            start.restype = wintypes.DWORD
            register = manager.RmRegisterResources
            register.argtypes = [
                wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
                wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
            ]
            register.restype = wintypes.DWORD
            get_list = manager.RmGetList
            get_list.argtypes = [
                wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
                ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            ]
            get_list.restype = wintypes.DWORD
            end = manager.RmEndSession
            end.argtypes = [wintypes.DWORD]
            end.restype = wintypes.DWORD

            class RmUniqueProcess(ctypes.Structure):
                _fields_ = [("process_id", wintypes.DWORD), ("start_time", wintypes.FILETIME)]

            class RmProcessInfo(ctypes.Structure):
                _fields_ = [
                    ("process", RmUniqueProcess),
                    ("app_name", wintypes.WCHAR * 256),
                    ("service_short_name", wintypes.WCHAR * 64),
                    ("application_type", wintypes.UINT),
                    ("app_status", wintypes.UINT),
                    ("session_id", wintypes.DWORD),
                    ("restartable", wintypes.BOOL),
                ]

            code = start(ctypes.byref(session), 0, session_key)
            if code != 0:
                raise OSError(code, _windows_error(code))
            try:
                files = (wintypes.LPCWSTR * 1)(str(target))
                code = register(session, 1, files, 0, None, 0, None)
                if code != 0:
                    raise OSError(code, _windows_error(code))
                needed = wintypes.UINT(0)
                count = wintypes.UINT(0)
                reboot_reasons = wintypes.DWORD(0)
                code = get_list(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reboot_reasons))
                if code == ERROR_MORE_DATA:
                    array = (RmProcessInfo * needed.value)()
                    count.value = len(array)
                    code = get_list(
                        session, ctypes.byref(needed), ctypes.byref(count),
                        array, ctypes.byref(reboot_reasons),
                    )
                    if code != 0:
                        # 包括再次 ERROR_MORE_DATA：不能把不完整结果误报为“无占用”。
                        raise OSError(code, _windows_error(code))
                    process_infos = list(array[:count.value])
                elif code != 0:
                    raise OSError(code, _windows_error(code))
            finally:
                end(session)
        except OSError as exc:
            code = exc.errno or 0
            result.requires_elevation = code in {ERROR_ACCESS_DENIED, 13}
            result.errors.append(f"Restart Manager 查询失败：{exc}")
            result.message = (
                "文件占用查询需要管理员权限；请以管理员身份运行后重试。"
                if result.requires_elevation else "文件占用方未知，未提供猜测结果。"
            )
            return result
        except Exception as exc:
            result.errors.append(f"Restart Manager 不可用或结构解析失败：{exc}")
            result.message = "文件占用方未知，未提供猜测结果。"
            return result

        result.confidence = Confidence.EXACT
        result.evidence.append("来源：Windows Restart Manager RmGetList")
        result.pids = sorted({int(item.process.process_id) for item in process_infos if item.process.process_id})
        names = {int(item.process.process_id): str(item.app_name) for item in process_infos}
        for pid in result.pids:
            user, elevation, error = resolve_process(pid, fallback_name=names.get(pid, ""))
            result.users.append(user)
            result.requires_elevation = result.requires_elevation or elevation
            if error:
                result.errors.append(error)
        if result.users:
            result.message = "文件占用进程由 Windows Restart Manager 精确报告。"
        else:
            result.message = "Restart Manager 本次未报告占用该文件的进程。"
        if result.requires_elevation:
            result.message += " 部分进程详情需要管理员权限。"
        return result

    find_users = inspect


FileInspector = RestartManagerInspector


def find_file_users(path: str | os.PathLike[str]) -> InspectionResult:
    return RestartManagerInspector().inspect(path)


def file_lock_users(path: str | os.PathLike[str]) -> InspectionResult:
    return find_file_users(path)
