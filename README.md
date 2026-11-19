# 本地模型管家（LocalLLM Manager）

一个运行在 Windows 上的本地大模型管理工具：扫描本机已有模型、查看运行状态、识别正在调用模型的应用、装入/释放显存资源，并提供场景化启动与模型下载入口。

> 当前状态：四页桌面界面、程序入口、多后端适配、调用方识别、服务层与 Agent 核心均已实现，并已通过单元测试与 Windows 单文件打包验证，可直接运行。

## 功能现状

| 模块 | 状态 | 说明 |
|---|---|---|
| 核心底座 | ✅ 已实现 | 配置、数据模型、领域异常、持久化设置、可取消后台任务、滚动日志 |
| 硬件快照与装入预检 | ✅ 已实现 | CPU/内存/GPU/Ollama 采集；装入前容量与后端状态粗估（不承诺装入成功） |
| 子进程监管 | ✅ 已实现 | 仅回收本工具创建的子进程，不使用 shell |
| SQLite 持久化 | ✅ 已实现 | 版本化迁移、模型/设置/使用统计表、线程本地连接、仓储层 |
| 模型扫描 | ✅ 已实现 | GGUF 解析、Ollama 库扫描、多格式识别、目录遍历、扫描服务 |
| 多后端适配 | ✅ 已实现 | Ollama 实装；LM Studio / llama.cpp / vLLM 检测 + 启动器 + 模型操作 |
| 调用方识别 | ✅ 已实现 | 端口反查、Windows Restart Manager 文件句柄反查、Ollama 日志关联 |
| 应用服务层 | ✅ 已实现 | 协调扫描/Provider/预检/存储/安全操作；下载拉取、导入导出、格式转换、安全删除 |
| Agent 核心（无 Qt） | ✅ 已实现 | 场景、会话持久化、改动建议解析、带确认执行的编程工作区 |
| 桌面界面（PySide6） | ✅ 已实现 | 模型管理 / 资源下载 / AI 场景 / 设置四页，含主题、表格状态恢复与操作日志 |
| 程序入口 `main.py` | ✅ 已实现 | 可直接从源码运行，或按 spec 打包为单文件 exe |

## 环境要求

- Windows 10 / 11
- Python 3.10+
- [Ollama](https://ollama.com/)（可选）：本项目只连接本机已安装的 Ollama，**不代装、不代下载 Ollama 本体**

## 快速开始

```bash
git clone <你的仓库地址>
cd <仓库目录>

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

> 首次启动使用本机既有 Ollama（`http://127.0.0.1:11434`），不会自动全盘扫描、自动采集硬件或自动启动任何推理服务。

## 目录结构

```
.
├── main.py                 # 程序入口
├── requirements.txt        # 运行依赖
├── requirements-dev.txt    # 开发依赖（pytest / pytest-qt / pyinstaller）
├── 本地模型管家.spec        # PyInstaller 打包配置
├── assets/                 # 应用图标与多尺寸图标
├── tools/                  # 图标构建脚本
├── tests/                  # 单元与界面冒烟测试
├── LICENSE
└── app/
    ├── core/               # config 配置 · models 数据结构 · errors 领域异常
    │                       # settings 持久设置 · tasks 可取消任务 · logging_config 日志
    │                       # hardware 硬件快照 · preflight 装入预检 · processes 子进程监管
    ├── scanner/            # gguf 解析 · ollama 库扫描 · formats 多格式 · walker 遍历 · service 服务
    ├── providers/          # base 接口 · ollama 实装 · lmstudio / llamacpp / vllm · t4 · registry 注册表
    ├── inspector/          # ports 端口反查 · files 句柄反查 · ollama_logs 日志关联
    ├── services/           # model_service 协调 · downloads 下载拉取 · transfer 导入导出/转换 · safety 安全删除
    ├── agent/              # 无 Qt Agent 核心：scenes 场景 · session 会话 · protocol 改动解析 · workspace 工作区
    ├── catalog/            # 内置资源清单
    ├── store/              # db 连接 · migrations 版本迁移 · repositories 仓储
    └── ui/                 # PySide6 界面：主窗口 · 模型管理 · 资源下载 · AI 场景 · 设置 · 主题
```

运行期数据（模型库缓存、日志）默认写入 `%APPDATA%\LLMManager`，不会污染仓库目录。

## T4 Provider 配置

可选的 T4 后备 Provider 默认不启用外部脚本。若需使用，请通过环境变量指定启停脚本路径（未配置时只读取已运行服务，不会执行任何脚本）：

```powershell
$env:LLM_MANAGER_T4_START_SCRIPT = "D:\path\to\start_t4.ps1"
$env:LLM_MANAGER_T4_STOP_SCRIPT  = "D:\path\to\stop_t4.ps1"
```

该 Provider 仅接受本机 `127.0.0.1:8712` 上、且健康接口报告为 Tesla T4 的服务，所有启动均由用户手动确认。

## 开发

```bash
pip install -r requirements-dev.txt
pytest
```

## 免责声明

- 本项目为独立第三方工具，与 Ollama、Qwen、Cline、Roo Code、Aider、Open Interpreter、OpenHands、剪映等产品及公司**无任何隶属或授权关系**，相关名称与商标归各自所有者。
- 本项目不打包、不分发任何第三方模型权重或商业软件，仅提供本机已有资源的索引与本机接口调用。
- 模型许可证各不相同，使用与再分发前请自行确认对应模型的许可条款。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方依赖的许可证以其各自声明为准（PySide6 为 LGPLv3）。
