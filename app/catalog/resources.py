"""内置中文资源目录；未知版本、大小与哈希不编造。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.models import DictSerializable


@dataclass(frozen=True)
class CatalogCategory(DictSerializable):
    id: str
    name: str
    description: str


@dataclass(frozen=True)
class CatalogResource(DictSerializable):
    id: str
    display_name: str
    category: str
    kind: str                       # model / software / agent
    description: str
    delivery: str                   # ollama / https-file / official-page / builtin
    model_ref: str = ""
    download_url: str = ""          # 只有 https-file 才是直接文件，网页绝不伪装成安装包
    official_url: str = ""
    source_url: str = ""
    license: str = "见官方页面"
    online_required: bool = True     # 首次获取是否需要网络
    offline_capable: bool = True     # 获取并配置后是否可离线使用
    requires_install: bool = False
    required: bool = False           # 仅本工具内置 Agent 是场景必需，外部软件均可选
    size_bytes: int = 0              # 0 = 官方目录未声明，下载前不猜测
    sha256: str = ""                # 空 = 官方目录未提供，不生成预期哈希
    filename: str = ""
    version: str = ""               # 空 = 不绑定/不编造版本
    tags: tuple[str, ...] = field(default_factory=tuple)


def _resource(**values: Any) -> CatalogResource:
    values.setdefault("official_url", values.get("source_url", ""))
    values.setdefault("source_url", values.get("official_url", ""))
    return CatalogResource(**values)


BUILTIN_CATEGORIES: tuple[CatalogCategory, ...] = (
    CatalogCategory("general", "通用 Agent", "内置对话、模型检查与确认式工具调用，不依赖外部 Agent。"),
    CatalogCategory("models", "大语言模型", "可通过 Ollama 获取的本地模型，以及本机推理配套软件。"),
    CatalogCategory("video", "视频理解 · 生成 · 剪辑", "内置视频 Agent，配套视觉模型、生成工作台和剪辑软件。"),
    CatalogCategory("coding", "AI 编程", "代码检索、阅读、方案与差异确认均在本工具内完成，编辑器和运行时按需选择。"),
    CatalogCategory("maintenance", "AI 工程维护", "内置维护对话与逐条命令确认，终端和 Shell 为可选系统配套。"),
)


BUILTIN_RESOURCES: tuple[CatalogResource, ...] = (
    _resource(
        id="agent-general", display_name="通用本地 Agent", category="general", kind="agent",
        description="本工具内置的独立聊天窗、流式本地推理与用户确认组件。",
        delivery="builtin", license="随本工具许可证", online_required=False, required=True,
        tags=("内置", "对话"),
    ),
    _resource(
        id="agent-video", display_name="视频理解 / 生成剪辑 Agent", category="video", kind="agent",
        description="视频场景对话始终在本工具内完成；生成或剪辑软件仅按需由用户启动。",
        delivery="builtin", license="随本工具许可证", online_required=False, required=True,
        tags=("内置", "视频"),
    ),
    _resource(
        id="agent-coding", display_name="AI 编程 Agent", category="coding", kind="agent",
        description="内置工作区选择、文件检索阅读、改动方案和差异预览；确认后才写入，不启动外部编程 Agent。",
        delivery="builtin", license="随本工具许可证", online_required=False, required=True,
        tags=("内置", "编程", "差异确认"),
    ),
    _resource(
        id="agent-maintenance", display_name="AI 工程维护 Agent", category="maintenance", kind="agent",
        description="内置维护面板，默认生成或复制命令；可控 CLI 的逐条执行也必须由用户确认。",
        delivery="builtin", license="随本工具许可证", online_required=False, required=True,
        tags=("内置", "工程维护", "命令确认"),
    ),
    _resource(
        id="ollama-qwen25-3b", display_name="Qwen2.5 3B（Ollama）", category="models", kind="model",
        description="较小规模的通用人称对话与文本模型选择；实际能力、上下文和许可证以官方模型页为准。",
        delivery="ollama", model_ref="qwen2.5:3b", official_url="https://ollama.com/library/qwen2.5",
        source_url="https://ollama.com/library/qwen2.5", tags=("Ollama", "文本"),
    ),
    _resource(
        id="ollama-qwen25-7b", display_name="Qwen2.5 7B（Ollama）", category="models", kind="model",
        description="通用本地文本模型；拉取由本机 Ollama 执行，外网仓库可用性需单独检查。",
        delivery="ollama", model_ref="qwen2.5:7b", official_url="https://ollama.com/library/qwen2.5",
        source_url="https://ollama.com/library/qwen2.5", license="Apache-2.0", tags=("Ollama", "文本"),
    ),
    _resource(
        id="ollama-llama31-8b", display_name="Llama 3.1 8B（Ollama）", category="models", kind="model",
        description="Meta Llama 3.1 系列本地模型条目；不声明未检测到的量化版本或文件大小。",
        delivery="ollama", model_ref="llama3.1:8b", official_url="https://ollama.com/library/llama3.1",
        source_url="https://ollama.com/library/llama3.1", license="Llama 3.1 Community License", tags=("Ollama", "文本"),
    ),
    _resource(
        id="ollama-qwen25vl-3b", display_name="Qwen2.5-VL 3B（视频/视觉理解）", category="video", kind="model",
        description="视觉语言模型，可供视频抽帧理解等流程选择；不等于视频生成模型，实际输入能力以官方说明为准。",
        delivery="ollama", model_ref="qwen2.5vl:3b", official_url="https://ollama.com/library/qwen2.5vl",
        source_url="https://ollama.com/library/qwen2.5vl", tags=("Ollama", "视觉", "视频理解"),
    ),
    _resource(
        id="hf-hunyuan-video", display_name="HunyuanVideo 官方模型页", category="video", kind="model",
        description="腾讯混元视频生成模型的官方仓库页面；仅提供来源入口，不猜测单个权重文件或哈希。",
        delivery="official-page", official_url="https://huggingface.co/tencent/HunyuanVideo",
        source_url="https://huggingface.co/tencent/HunyuanVideo", tags=("视频生成", "官方仓库"),
    ),
    _resource(
        id="hf-wan21-t2v", display_name="Wan2.1 T2V 14B 官方模型页", category="video", kind="model",
        description="Wan-AI 文生视频模型官方仓库页面；下载文件、磁盘空间和许可证须以页面为准。",
        delivery="official-page", official_url="https://huggingface.co/Wan-AI/Wan2.1-T2V-14B",
        source_url="https://huggingface.co/Wan-AI/Wan2.1-T2V-14B", tags=("视频生成", "官方仓库"),
    ),
    _resource(
        id="software-ollama", display_name="Ollama", category="models", kind="software",
        description="本机模型运行时。本工具只连接既有本机服务，不自动安装；此为官方安装包直链。",
        delivery="https-file", download_url="https://ollama.com/download/OllamaSetup.exe",
        official_url="https://ollama.com/download", source_url="https://github.com/ollama/ollama",
        license="MIT", requires_install=True, filename="OllamaSetup.exe", tags=("推理后端", "可选"),
    ),
    _resource(
        id="software-lmstudio", display_name="LM Studio", category="models", kind="software",
        description="可选桌面模型运行时；仅提供官方下载页面，不将网页或第三方镜像当作安装包。",
        delivery="official-page", official_url="https://lmstudio.ai/download",
        source_url="https://lmstudio.ai/", license="专有许可（以官方条款为准）",
        requires_install=True, tags=("推理后端", "可选"),
    ),
    _resource(
        id="software-comfyui", display_name="ComfyUI", category="video", kind="software",
        description="节点式生成工作台，可作为视频生成配套；官方发布页提供源码与可用发行物，不是必需 Agent。",
        delivery="official-page", official_url="https://github.com/comfyanonymous/ComfyUI/releases/latest",
        source_url="https://github.com/comfyanonymous/ComfyUI", license="GPL-3.0",
        requires_install=False, tags=("视频生成", "便携/源码"),
    ),
    _resource(
        id="software-shotcut", display_name="Shotcut", category="video", kind="software",
        description="可选开源视频剪辑软件；只跳转官方发布页，安装包由用户核对后选择。",
        delivery="official-page", official_url="https://github.com/mltframework/shotcut/releases/latest",
        source_url="https://www.shotcut.org/", license="GPL-3.0", requires_install=True,
        tags=("视频剪辑", "官方发布"),
    ),
    _resource(
        id="software-kdenlive", display_name="Kdenlive", category="video", kind="software",
        description="可选开源视频剪辑软件；Windows 安装方式和版本以官方下载页为准。",
        delivery="official-page", official_url="https://kdenlive.org/en/download/",
        source_url="https://kdenlive.org/", license="GPL-3.0", requires_install=True,
        tags=("视频剪辑", "官方页面"),
    ),
    _resource(
        id="software-blender", display_name="Blender", category="video", kind="software",
        description="可选三维、视频序列与合成工具；官方提供便携和安装形式，本工具不替用户选择。",
        delivery="official-page", official_url="https://www.blender.org/download/",
        source_url="https://www.blender.org/", license="GPL-2.0-or-later", requires_install=False,
        tags=("视频制作", "合成", "便携可选"),
    ),
    _resource(
        id="software-ffmpeg", display_name="FFmpeg", category="video", kind="software",
        description="可选音视频转码、抽帧和处理工具；官方页面列出源码及各平台构建入口，本工具不选第三方镜像。",
        delivery="official-page", official_url="https://ffmpeg.org/download.html",
        source_url="https://ffmpeg.org/", license="LGPL/GPL（取决于构建，以官方说明为准）",
        requires_install=False, tags=("抽帧", "转码", "命令行"),
    ),
    _resource(
        id="software-vscode", display_name="Visual Studio Code", category="coding", kind="software",
        description="可选代码编辑器；内置编程 Agent 不依赖它。此地址是微软官方稳定版 Windows 用户安装包重定向。",
        delivery="https-file", download_url="https://code.visualstudio.com/sha/download?build=stable&os=win32-x64-user",
        official_url="https://code.visualstudio.com/download", source_url="https://code.visualstudio.com/",
        license="Microsoft 产品许可（以官方页面为准）", requires_install=True,
        filename="VSCodeUserSetup.exe", tags=("编辑器", "可选"),
    ),
    _resource(
        id="software-git", display_name="Git for Windows", category="coding", kind="software",
        description="可选版本控制工具；内置 Agent 的差异预览不等于 Git，也不自动执行 Git 命令。",
        delivery="official-page", official_url="https://git-scm.com/download/win",
        source_url="https://git-scm.com/", license="GPL-2.0", requires_install=True,
        tags=("版本控制", "可选"),
    ),
    _resource(
        id="software-python", display_name="Python", category="coding", kind="software",
        description="可选脚本与工程运行时；仅提供 Python 官方 Windows 下载页面，不自动安装或修改 PATH。",
        delivery="official-page", official_url="https://www.python.org/downloads/windows/",
        source_url="https://www.python.org/", license="PSF License", requires_install=True,
        tags=("运行时", "可选"),
    ),
    _resource(
        id="software-powershell", display_name="PowerShell", category="maintenance", kind="software",
        description="可选工程维护 Shell；内置 Agent 默认只生成或复制命令，不因安装此软件而自动执行。",
        delivery="official-page", official_url="https://github.com/PowerShell/PowerShell/releases/latest",
        source_url="https://github.com/PowerShell/PowerShell", license="MIT", requires_install=True,
        tags=("Shell", "命令确认"),
    ),
    _resource(
        id="software-windowsterminal", display_name="Windows Terminal", category="maintenance", kind="software",
        description="可选终端宿主；官方最新发布页仅用于用户选择安装包，不作为任何 Agent 的依赖。",
        delivery="official-page", official_url="https://github.com/microsoft/terminal/releases/latest",
        source_url="https://github.com/microsoft/terminal", license="MIT", requires_install=True,
        tags=("终端", "可选"),
    ),
)


def builtin_resources() -> list[CatalogResource]:
    return list(BUILTIN_RESOURCES)


def builtin_categories() -> list[CatalogCategory]:
    return list(BUILTIN_CATEGORIES)
