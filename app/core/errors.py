"""可由界面统一分类处理的领域异常。"""
from __future__ import annotations


class ModelManagerError(Exception):
    """本地模型管家领域异常基类。"""


class BackendNotInstalledError(ModelManagerError):
    """所需后端未安装或找不到可执行文件。"""


class BackendOfflineError(ModelManagerError):
    """本机后端服务未运行或不可访问。"""


class UnsupportedOperationError(ModelManagerError):
    """当前平台、后端或模型不支持该操作。"""


class ElevationRequiredError(ModelManagerError):
    """操作需要管理员权限，调用方应请求提权后重试。"""


class OperationCancelledError(ModelManagerError):
    """任务通过取消令牌主动终止。"""
