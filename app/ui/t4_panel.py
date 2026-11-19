"""T4 后备推理服务面板：放在设置页，按需手动启停。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QWidget,
)

from ..providers.base import ProviderStatus
from ..providers.t4 import T4Provider


class T4Panel(QGroupBox):
    """只负责展示与按钮；启动/停止/刷新由模型页持有真实逻辑。"""

    start_requested = Signal()
    stop_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, provider: T4Provider, parent: QWidget | None = None):
        super().__init__("T4 按需推理服务（手动启停）", parent)
        self.provider = provider
        grid = QGridLayout(self)
        grid.setColumnStretch(0, 1)
        description = QLabel(
            "平常只连接本机既有 Ollama；T4 只是 Ollama 不可用时的后备，"
            "必须在这里手动启动，用完手动停止，不会自动启动或开机自启。"
        )
        description.setWordWrap(True)
        grid.addWidget(description, 0, 0, 1, 3)
        self.service_label = QLabel("服务状态：尚未检测（检测不会执行脚本）")
        self.service_label.setWordWrap(True)
        grid.addWidget(self.service_label, 1, 0, 1, 3)
        self.scripts_label = QLabel()
        self.scripts_label.setWordWrap(True)
        grid.addWidget(self.scripts_label, 2, 0, 1, 3)
        self.gpu_label = QLabel("允许的 Tesla T4 显存：尚未采集")
        self.gpu_label.setWordWrap(True)
        grid.addWidget(self.gpu_label, 3, 0, 1, 3)
        self.start_button = QPushButton("手动启动 T4")
        self.stop_button = QPushButton("手动停止 T4")
        self.refresh_button = QPushButton("刷新状态")
        grid.addWidget(self.start_button, 4, 0)
        grid.addWidget(self.stop_button, 4, 1)
        grid.addWidget(self.refresh_button, 4, 2)
        policy = QLabel("只允许索引非 0 且名称精确为 Tesla T4 的显卡；明确排除 GPU0 / GTX1080。")
        policy.setWordWrap(True)
        policy.setStyleSheet("color: #d8b56a;")
        grid.addWidget(policy, 5, 0, 1, 3)
        self.start_button.clicked.connect(self.start_requested)
        self.stop_button.clicked.connect(self.stop_requested)
        self.refresh_button.clicked.connect(self.refresh_requested)
        self.set_scripts(provider.start_script, provider.stop_script)

    def set_scripts(self, start_script: str, stop_script: str) -> None:
        start_exists = Path(start_script).is_file()
        stop_exists = Path(stop_script).is_file()
        self.scripts_label.setText(
            f"启动脚本：{'已就绪' if start_exists else '缺失'}；"
            f"停止脚本：{'已就绪' if stop_exists else '缺失'}"
        )
        self.scripts_label.setToolTip(f"启动：{start_script}\n停止：{stop_script}")

    def set_status(self, status: ProviderStatus) -> None:
        self.service_label.setText(
            f"服务状态：{'运行中（健康检查已通过）' if status.running else '未运行 / 未就绪'}；"
            f"{status.message or status.error}"
        )
        self.service_label.setStyleSheet(f"color: {'#63d68b' if status.running else '#d8b56a'};")
        self.service_label.setToolTip(status.error or status.message)
        gpu = status.details.get("gpu", {})
        rows = gpu.get("gpus", [])
        if rows:
            lines = [
                f"GPU{row['index']} {row['name']}：已用 {row['memory_used_mib']} MiB / "
                f"总计 {row['memory_total_mib']} MiB / 空闲 {row['memory_free_mib']} MiB"
                for row in rows
            ]
            self.gpu_label.setText("Tesla T4 实际显存：\n" + "\n".join(lines))
        else:
            self.gpu_label.setText(
                "Tesla T4 实际显存：未获得；"
                + str(gpu.get("error") or gpu.get("message") or "状态未返回显存数据")
            )
        self.gpu_label.setToolTip(str(gpu.get("policy") or "GPU0 / GTX1080 禁止使用"))

    def set_running(self, running: bool | None) -> None:
        self.start_button.setEnabled(running is not True)
        self.stop_button.setEnabled(running is True)

    def set_busy(self, busy: bool, shutdown: bool = False) -> None:
        if busy or shutdown:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.refresh_button.setEnabled(False)
        else:
            self.refresh_button.setEnabled(True)
