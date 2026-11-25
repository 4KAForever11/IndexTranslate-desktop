"""Glossary editor: term → preferred translation, injected into every prompt."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    TitleLabel,
)

from ..core.glossary_db import GlossaryDB
from .theme import Card, MutedLabel, PageWidget

try:  # fluent's styled table; plain Qt table is a safe fallback
    from qfluentwidgets import TableWidget
except ImportError:  # pragma: no cover
    from PyQt6.QtWidgets import QTableWidget as TableWidget  # type: ignore


class GlossaryPage(PageWidget):
    changed = pyqtSignal()

    def __init__(self, glossary_db: GlossaryDB, parent=None) -> None:
        super().__init__(parent)
        self.glossary_db = glossary_db
        self._loading = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(TitleLabel("术语表"))
        self.count_label = MutedLabel("0 条", size=12)
        header.addWidget(self.count_label)
        header.addStretch(1)
        self.search = SearchLineEdit()
        self.search.setPlaceholderText("筛选术语")
        self.search.setMinimumWidth(200)
        self.import_button = PushButton("导入")
        self.export_button = PushButton("导出")
        self.delete_button = PushButton("删除所选")
        for widget in (self.search, self.import_button, self.export_button, self.delete_button):
            header.addWidget(widget)
        outer.addLayout(header)

        outer.addWidget(
            MutedLabel(
                "术语表会以「硬性要求」的形式附加到每次翻译，适合人名、品牌名、产品名。"
                "支持「原文→译文」或「原文=译文」格式的文本文件。",
                size=12,
            )
        )

        add_card = Card(radius=8)
        add_layout = QHBoxLayout(add_card)
        add_layout.setContentsMargins(14, 10, 14, 10)
        add_layout.setSpacing(8)
        self.term_edit = LineEdit()
        self.term_edit.setPlaceholderText("术语，例如：光遇")
        self.term_edit.setFixedHeight(34)
        self.value_edit = LineEdit()
        self.value_edit.setPlaceholderText("指定译法，例如：Sky: Children of the Light")
        self.value_edit.setFixedHeight(34)
        self.add_button = PrimaryPushButton("添加")
        self.add_button.setMinimumWidth(96)
        self.add_button.setFixedHeight(34)
        add_layout.addWidget(self.term_edit, 2)
        add_layout.addWidget(self.value_edit, 3)
        add_layout.addWidget(self.add_button)
        outer.addWidget(add_card)

        self.table = TableWidget(self)
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["术语", "译法"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        if hasattr(self.table, "setBorderVisible"):
            self.table.setBorderVisible(True)
        if hasattr(self.table, "setBorderRadius"):
            self.table.setBorderRadius(10)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        header_view = self.table.horizontalHeader()
        header_view.setFixedHeight(38)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        outer.addWidget(self.table, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self.refresh)

        self.add_button.clicked.connect(self._add_term)
        self.term_edit.returnPressed.connect(self._add_term)
        self.value_edit.returnPressed.connect(self._add_term)
        self.search.textChanged.connect(lambda _: self._debounce.start())
        self.delete_button.clicked.connect(self._delete_selected)
        self.import_button.clicked.connect(self._import_file)
        self.export_button.clicked.connect(self._export_file)
        self.table.itemChanged.connect(self._on_item_changed)

    # ------------------------------------------------------------------- table
    def refresh(self) -> None:
        keyword = self.search.text().strip().lower()
        terms = [
            (term, translation)
            for term, translation in self.glossary_db.all()
            if not keyword or keyword in term.lower() or keyword in translation.lower()
        ]
        self._loading = True
        try:
            self.table.setRowCount(len(terms))
            for row, (term, translation) in enumerate(terms):
                term_item = QTableWidgetItem(term)
                term_item.setFlags(term_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, 0, term_item)
                self.table.setItem(row, 1, QTableWidgetItem(translation))
        finally:
            self._loading = False
        self.count_label.setText(f"{self.glossary_db.count()} 条")

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 1:
            return
        term_item = self.table.item(item.row(), 0)
        if term_item is None:
            return
        term = term_item.text().strip()
        if not term:
            return
        self.glossary_db.upsert(term, item.text().strip())
        self.changed.emit()

    # ----------------------------------------------------------------- actions
    def _add_term(self) -> None:
        term = self.term_edit.text().strip()
        if not term:
            InfoBar.warning(
                title="缺少术语",
                content="请先填写要固定的术语原文。",
                duration=2500,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            return
        self.glossary_db.upsert(term, self.value_edit.text().strip())
        self.term_edit.clear()
        self.value_edit.clear()
        self.refresh()
        self.changed.emit()
        self.term_edit.setFocus()

    def _delete_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            InfoBar.warning(
                title="没有选中行",
                content="先在列表里选中要删除的术语。",
                duration=2500,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            return
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                self.glossary_db.delete(item.text())
        self.refresh()
        self.changed.emit()

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入术语表", "", "文本文件 (*.txt *.csv);;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            self._error(f"无法读取文件：{exc}")
            return
        text = ""
        for encoding in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            self._error("无法识别文件编码，请另存为 UTF-8 后重试。")
            return
        added = self.glossary_db.import_lines(text.splitlines())
        self.refresh()
        self.changed.emit()
        InfoBar.success(
            title="导入完成",
            content=f"已写入 {added} 条术语。",
            duration=2600,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _export_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出术语表", "术语表.txt", "文本文件 (*.txt);;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            Path(path).write_text(self.glossary_db.export_text(), encoding="utf-8")
        except OSError as exc:
            self._error(f"写入失败：{exc}")
            return
        InfoBar.success(
            title="导出完成",
            content="术语表已按「原文→译文」保存。",
            duration=2600,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _error(self, message: str) -> None:
        InfoBar.error(
            title="操作失败",
            content=message,
            duration=4000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )
