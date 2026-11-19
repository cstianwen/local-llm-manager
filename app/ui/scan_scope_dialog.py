"""扫描范围对话框：先选范围，再开始扫描。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.settings import Settings
from ..store.db import ModelStore

_SCOPE_ROLE = int(Qt.ItemDataRole.UserRole)


class ScanScopeDialog(QDialog):
    """勾选要扫描的位置；没有任何勾选时不能开始扫描。"""

    def __init__(
        self,
        store: ModelStore,
        available_drives: list[Path],
        settings: Settings,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.store = store
        self.settings = settings
        self._rows: list[str] = []
        self.setWindowTitle("选择扫描范围")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "只有勾选的位置会被遍历。系统目录（Windows、Program Files、ProgramData、"
            "回收站等）始终跳过，扫描只读并写入本机缓存。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("扫描位置", "是否存在", "上次扫描", "缓存模型"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        self.summary_label = QLabel("尚未选择任何位置")
        layout.addWidget(self.summary_label)

        buttons_row = QHBoxLayout()
        self.add_button = QPushButton("添加目录…")
        self.all_button = QPushButton("全选")
        self.none_button = QPushButton("清空")
        buttons_row.addWidget(self.add_button)
        buttons_row.addWidget(self.all_button)
        buttons_row.addWidget(self.none_button)
        buttons_row.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.start_button = QPushButton("开始扫描")
        buttons_row.addWidget(self.cancel_button)
        buttons_row.addWidget(self.start_button)
        layout.addLayout(buttons_row)
        self.add_button.clicked.connect(self._add_directory)
        self.all_button.clicked.connect(lambda: self._set_all(True))
        self.none_button.clicked.connect(lambda: self._set_all(False))
        self.cancel_button.clicked.connect(self.reject)
        self.start_button.clicked.connect(self.accept)
        self.table.itemChanged.connect(self._update_summary)
        self._populate(available_drives)

    def _row(self, label: str, path: Path, last_scanned: Any = None) -> None:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in {os.path.normcase(os.path.abspath(item)) for item in self._rows}:
            return
        self._rows.append(str(path))
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = QTableWidgetItem(f"{label}：{path}")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        selected = self._previously_selected(str(path))
        item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
        item.setData(_SCOPE_ROLE, str(path))
        item.setToolTip(str(path))
        self.table.setItem(row, 0, item)
        exists = Path(path).is_dir()
        exists_item = QTableWidgetItem("存在" if exists else "不存在")
        if not exists:
            exists_item.setForeground(Qt.GlobalColor.gray)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(row, 1, exists_item)
        scanned = (
            datetime.fromtimestamp(last_scanned).strftime("%Y-%m-%d %H:%M")
            if isinstance(last_scanned, (int, float)) and last_scanned
            else "未扫描"
        )
        self.table.setItem(row, 2, QTableWidgetItem(scanned))
        self.table.setItem(row, 3, QTableWidgetItem(str(self._cached_count(path))))

    def _populate(self, available_drives: list[Path]) -> None:
        home = Path.home()
        ollama = home / ".ollama" / "models"
        self._row("Ollama 模型库", ollama)
        for label, path in (
            ("LM Studio 模型库", home / ".cache" / "lm-studio" / "models"),
            ("LM Studio 模型库", home / ".lmstudio" / "models"),
        ):
            if path.is_dir():
                self._row(label, path)
        rows = {row["path"]: row for row in self.store.scan_roots.list()}
        for path in list(rows) + list(self.settings.get("scan_default_roots", []) or []):
            self._row("已添加目录", Path(str(path)), rows.get(str(path), {}).get("last_scanned_at"))
        for drive in available_drives:
            self._row("整个盘符", Path(drive))
        self._update_summary()

    def _previously_selected(self, path: str) -> bool:
        key = os.path.normcase(os.path.abspath(path))
        saved = self.settings.get("scan_scope_selection", [])
        if not isinstance(saved, list):
            return False
        return key in {os.path.normcase(os.path.abspath(str(item))) for item in saved}

    def _cached_count(self, path: Path) -> int:
        prefix = str(path).rstrip("\\/") + os.sep
        try:
            row = self.store.connection.execute(
                "SELECT COUNT(*) FROM models WHERE file_path LIKE ?", (prefix + "%",),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _add_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择要扫描的模型目录")
        if not directory:
            return
        added = list(self.settings.get("scan_default_roots", []) or [])
        if directory not in added:
            added.append(directory)
            self.settings.set("scan_default_roots", added)
        self.store.scan_roots.add(directory)
        self._row("已添加目录", Path(directory))
        self._set_checked(directory, True)
        self._update_summary()

    def _set_checked(self, path: str, checked: bool) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(_SCOPE_ROLE) == path:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _set_all(self, checked: bool) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def checked_roots(self) -> list[str]:
        roots: list[str] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                roots.append(str(item.data(_SCOPE_ROLE)))
        return roots

    def include_ollama(self) -> bool:
        ollama = os.path.normcase(os.path.abspath(str(Path.home() / ".ollama" / "models")))
        return any(os.path.normcase(os.path.abspath(root)) == ollama for root in self.checked_roots())

    def _update_summary(self, *_args: Any) -> None:
        count = len(self.checked_roots())
        self.summary_label.setText(
            f"已选 {count} 个位置" if count else "至少勾选一个位置后才能开始扫描"
        )
        self.start_button.setEnabled(count > 0)

    def accept(self) -> None:
        roots = self.checked_roots()
        if not roots:
            QMessageBox.information(self, "未选择范围", "请至少勾选一个扫描位置。")
            return
        self.settings.set("scan_scope_selection", roots)
        super().accept()
