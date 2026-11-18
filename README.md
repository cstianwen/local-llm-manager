# 本地模型管家（LocalLLM Manager）

一个运行在 Windows 上的本地大模型管理工具：扫描本机已有模型、查看运行状态、识别正在调用模型的应用、装入/释放显存资源，并提供场景化启动与模型下载入口。

> 当前处于**开发中**，核心数据结构、扫描解析与持久化层已完成，界面与后端适配正在实现。

## 功能规划

| 模块 | 状态 | 说明 |
|---|---|---|
| 全局配置 | ✅ 已实现 | 后端地址、扫描阈值、跳过目录、数据目录 |
| 数据模型与领域异常 | ✅ 已实现 | ModelInfo / RunningModel / AppUser，统一异常分类 |
| SQLite 持久化 | ✅ 已实现 | 版本化迁移、模型表、设置表、使用统计表、仓储层 |
| 设置持久化 | ✅ 已实现 | 基于 settings 表的键值配置，带默认值 |
| 可取消后台任务 | ✅ 已实现 | QThreadPool/QRunnable 协作式取消 + 进度回调 |
| 模型扫描（GGUF 解析） | ✅ 已实现 | 只读文件头，提取架构、参数量、量化、上下文长度 |
| 多格式识别（safetensors / pytorch bin / Ollama 库） | 🚧 开发中 | 广嗅探，识别不准的标为未知而不丢弃 |
| 多后端适配（Ollama / LM Studio / llama.cpp） | 🚧 开发中 | 统一 Provider 接口，Ollama 优先 |
| 调用方识别（端口反查 + 句柄反查） | 🚧 开发中 | 端口→PID→进程名/路径 |
| 桌面界面（PySide6） | 🚧 开发中 | 模型管理 / AI 场景 / 资源下载 / 设置 |
| 装入与释放、装入前预检 | 🚧 开发中 | 目标设备选择、显存/内存可行性评估 |

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

> `main.py` 随界面层一并提供，当前仓库尚未包含可运行入口。

## 目录结构

```
.
├── main.py                 # 程序入口（待提交）
├── requirements.txt        # 运行依赖
├── requirements-dev.txt    # 开发依赖（pytest / pytest-qt / pyinstaller）
├── LICENSE
└── app/
    ├── core/               # config 配置 · models 数据结构 · errors 领域异常
    │                       # settings 持久设置 · tasks 可取消后台任务 · logging_config 日志
    ├── scanner/            # 扫描与格式识别（gguf.py 已实现）
    ├── providers/          # 多后端适配
    ├── inspector/          # 调用方反查
    ├── store/              # db 连接 · migrations 版本迁移 · repositories 仓储
    └── ui/                 # PySide6 界面
```

运行期数据（模型库缓存、日志）默认写入 `%APPDATA%\LLMManager`，不会污染仓库目录。

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
