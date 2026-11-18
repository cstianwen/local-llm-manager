"""把模型文本解析为受限改动建议；解析阶段绝不访问磁盘或执行命令。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .models import ChangeOperation


_FENCE = re.compile(r"^```([^\n`]*)[\t ]*\n(.*?)^```[\t ]*$", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class ChangeSuggestion:
    path: str = ""
    operation: ChangeOperation | None = None
    content: str | None = None
    unified_diff: str = ""


@dataclass
class SuggestionParseResult:
    text: str
    suggestions: list[ChangeSuggestion] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.suggestions)


def _json_suggestions(payload: object) -> tuple[list[ChangeSuggestion], list[str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("changes"), list):
        return [], ["JSON 改动协议必须是包含 changes 数组的对象"]
    suggestions: list[ChangeSuggestion] = []
    issues: list[str] = []
    for index, item in enumerate(payload["changes"], 1):
        if not isinstance(item, dict):
            issues.append(f"第 {index} 项改动不是对象")
            continue
        try:
            operation = ChangeOperation(str(item.get("operation", "")).lower())
        except ValueError:
            issues.append(f"第 {index} 项 operation 仅支持 create/modify/delete")
            continue
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not path.strip():
            issues.append(f"第 {index} 项缺少有效 path")
            continue
        if operation == ChangeOperation.DELETE:
            if content is not None:
                issues.append(f"第 {index} 项删除操作不能包含 content")
                continue
        elif not isinstance(content, str):
            issues.append(f"第 {index} 项新增/修改操作必须包含字符串 content")
            continue
        suggestions.append(ChangeSuggestion(path=path, operation=operation, content=content))
    return suggestions, issues


def parse_change_suggestions(text: str) -> SuggestionParseResult:
    """识别 fenced diff/patch 或严格 JSON；普通与无效文本不产生改动。"""
    if not isinstance(text, str):
        raise TypeError("模型回复必须是字符串")
    result = SuggestionParseResult(text=text)
    fenced_ranges: list[tuple[int, int]] = []
    for match in _FENCE.finditer(text):
        language = match.group(1).strip().lower()
        body = match.group(2)
        if language not in {"diff", "patch", "json"}:
            continue
        fenced_ranges.append(match.span())
        if language in {"diff", "patch"}:
            if "--- " not in body or "+++ " not in body or "@@" not in body:
                result.issues.append("diff 代码块缺少文件头或 hunk，已仅作为文本处理")
            else:
                result.suggestions.append(ChangeSuggestion(unified_diff=body))
            continue
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            result.issues.append(f"JSON 改动协议无效：{exc}")
            continue
        items, issues = _json_suggestions(payload)
        result.suggestions.extend(items)
        result.issues.extend(issues)

    stripped = text.strip()
    if not fenced_ranges and stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError) as exc:
            result.issues.append(f"JSON 改动协议无效：{exc}")
        else:
            items, issues = _json_suggestions(payload)
            result.suggestions.extend(items)
            result.issues.extend(issues)
    elif not result.suggestions and re.search(r"^```(?:diff|patch|json)\b", text, re.MULTILINE):
        result.issues.append("改动代码块未闭合或格式无效，未生成任何写入计划")
    return result
