"""后台任务阶段与状态的中文文案，界面不再直接展示内部标识。"""
from __future__ import annotations

from typing import Any

PHASE_TEXT = {
    "preparing": "准备中",
    "scanning": "遍历目录",
    "parsing": "解析模型",
    "ollama": "读取本机模型库",
    "completed": "已完成",
    "downloading": "下载中",
    "verifying": "校验文件",
    "cancelling": "取消中",
    "failed": "失败",
    "cancelled": "已取消",
    "pending": "等待中",
    "idle": "空闲",
}


def phase_text(value: Any, default: str = "进行中") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return PHASE_TEXT.get(text.casefold(), text or default)
