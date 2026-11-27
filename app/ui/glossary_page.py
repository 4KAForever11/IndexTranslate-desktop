"""Glossary editor: term → preferred translation, injected into every prompt.

术语按「分组 / 场景」组织：同一句原文在不同领域可以有不同译法，只有启用的
分组会参与注入，注入时还只挑原文真正命中的词条。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QTableWidgetItem,
    QVBoxLayout,
)
from qfluentwidgets import (
    ComboBox,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    SwitchButton,
    TitleLabel,
)

from ..core import settings
from ..core.glossary_db import DEFAULT_GROUP, GlossaryDB
from .theme import Card, MutedLabel, PageWidget

try:  # fluent's styled table; plain Qt table is a safe fallback
    from qfluentwidgets import TableWidget
except ImportError:  # pragma: no cover
    from PyQt6.QtWidgets import QTableWidget as TableWidget  # type: ignore


ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


class GlossaryPage(PageWidget):
    changed = pyqtSignal()

    def __init__(self, glossary_db: GlossaryDB, parent=None) -> None:
        super().__init__(parent)
        self.glossary_db = glossary_db
        self._loading = False
        self._current_group = DEFAULT_GROUP
        self._build_ui()
        self._reload_groups()
        self.refresh()

    # --------------------------------------------------------------------- UI
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
        self.search.setMinimumWidth(180)
        self.import_button = PushButton("导入")
        self.export_button = PushButton("导出")
        self.delete_button = PushButton("删除所选")
        for widget in (self.search, self.import_button, self.export_button, self.delete_button):
            header.addWidget(widget)
        outer.addLayout(header)

        group_card = Card(radius=8)
        group_layout = QHBoxLayout(group_card)
        group_layout.setContentsMargins(14, 10, 14, 10)
        group_layout.setSpacing(10)

        group_layout.addWidget(MutedLabel("分组 / 场景", size=12))
        self.group_combo = ComboBox()
        self.group_combo.setMinimumWidth(160)
        self.group_combo.setToolTip("不同领域可以各用一套术语，互不干扰")
        group_layout.addWidget(self.group_combo)

        self.group_switch = SwitchButton()
        self.group_switch.setOnText("启用")
        self.group_switch.setOffText("停用")
        self.group_switch.setToolTip("停用的分组不会注入提示词")
        group_layout.addWidget(self.group_switch)

        self.new_group_button = PushButton("新建分组")
        self.rename_group_button = PushButton("重命名")
        self.delete_group_button = PushButton("删除分组")
        group_layout.addStretch(1)
        for widget in (self.new_group_button, self.rename_group_button, self.delete_group_button):
            group_layout.addWidget(widget)
        outer.addWidget(group_card)

        outer.addWidget(
            MutedLabel(
                "只有启用的分组、并且原文里真的出现过的术语才会写进提示词，既避免跨领域冲突，也减少首字延迟。",
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
        for column in range(2):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setTextAlignment(ALIGN_LEFT)
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
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        self.group_switch.checkedChanged.connect(self._on_group_toggled)
        self.new_group_button.clicked.connect(self._create_group)
        self.rename_group_button.clicked.connect(self._rename_group)
        self.delete_group_button.clicked.connect(self._delete_group)

    # ------------------------------------------------------------------ groups
    @property
    def current_group(self) -> str:
        return self._current_group

    def _reload_groups(self, select: str = "") -> None:
        groups = self.glossary_db.groups()
        if not groups:
            self.glossary_db.ensure_group(DEFAULT_GROUP)
            groups = self.glossary_db.groups()
        wanted = select or str(settings.get("glossary_group", DEFAULT_GROUP) or DEFAULT_GROUP)

        blocked = self.group_combo.blockSignals(True)
        self.group_combo.clear()
        for name, enabled in groups:
            self.group_combo.addItem(f"{name}{'' if enabled else '（已停用）'}", userData=name)
        index = self.group_combo.findData(wanted)
        if index < 0:
            index = 0
        self.group_combo.setCurrentIndex(index)
        self.group_combo.blockSignals(blocked)

        self._current_group = str(self.group_combo.currentData() or DEFAULT_GROUP)
        enabled_map = dict(groups)
        blocked = self.group_switch.blockSignals(True)
        self.group_switch.setChecked(bool(enabled_map.get(self._current_group, True)))
        self.group_switch.blockSignals(blocked)
        settings.set("glossary_group", self._current_group)
        # 新建 / 重命名 / 删除 / 停用分组后调用到这里，表格必须跟着切换
        self.refresh()

    def _on_group_changed(self, _index: int) -> None:
        self._current_group = str(self.group_combo.currentData() or DEFAULT_GROUP)
        settings.set("glossary_group", self._current_group)
        settings.save()
        enabled_map = dict(self.glossary_db.groups())
        blocked = self.group_switch.blockSignals(True)
        self.group_switch.setChecked(bool(enabled_map.get(self._current_group, True)))
        self.group_switch.blockSignals(blocked)
        self.refresh()

    def _on_group_toggled(self, checked: bool) -> None:
        self.glossary_db.set_group_enabled(self._current_group, checked)
        self._reload_groups(self._current_group)
        self.changed.emit()
        InfoBar.success(
            title="已启用" if checked else "已停用",
            content=f"「{self._current_group}」分组{'会' if checked else '不会'}参与提示词注入。",
            duration=2200,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _create_group(self) -> None:
        name, ok = QInputDialog.getText(self, "新建分组", "分组名称（例如：游戏、开发、医学）")
        name = (name or "").strip()
        if not ok or not name:
            return
        self.glossary_db.ensure_group(name)
        self._reload_groups(name)
        self.refresh()
        self.changed.emit()

    def _rename_group(self) -> None:
        old = self._current_group
        name, ok = QInputDialog.getText(self, "重命名分组", "新的分组名称", text=old)
        name = (name or "").strip()
        if not ok or not name or name == old:
            return
        self.glossary_db.rename_group(old, name)
        self._reload_groups(name)
        self.refresh()
        self.changed.emit()

    def _delete_group(self) -> None:
        group = self._current_group
        box = MessageBox(
            "删除分组",
            f"将删除「{group}」分组及其中的 {self.glossary_db.count(group)} 条术语，此操作不可撤销。",
            self.window(),
        )
        if not box.exec():
            return
        self.glossary_db.delete_group(group)
        self._reload_groups(DEFAULT_GROUP)
        self.refresh()
        self.changed.emit()

    # ------------------------------------------------------------------- table
    def refresh(self) -> None:
        keyword = self.search.text().strip().lower()
        terms = [
            (term, translation)
            for term, translation in self.glossary_db.all(self._current_group)
            if not keyword or keyword in term.lower() or keyword in translation.lower()
        ]
        self._loading = True
        try:
            self.table.setRowCount(len(terms))
            for row, (term, translation) in enumerate(terms):
                term_item = QTableWidgetItem(term)
                term_item.setFlags(term_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                term_item.setTextAlignment(ALIGN_LEFT)
                self.table.setItem(row, 0, term_item)
                value_item = QTableWidgetItem(translation)
                value_item.setTextAlignment(ALIGN_LEFT)
                self.table.setItem(row, 1, value_item)
        finally:
            self._loading = False
        total = self.glossary_db.count()
        current = self.glossary_db.count(self._current_group)
        self.count_label.setText(f"{self._current_group} {current} 条 · 全部 {total} 条")

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 1:
            return
        term_item = self.table.item(item.row(), 0)
        if term_item is None:
            return
        term = term_item.text().strip()
        if not term:
            return
        self.glossary_db.upsert(term, item.text().strip(), self._current_group)
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
        self.glossary_db.upsert(term, self.value_edit.text().strip(), self._current_group)
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
                self.glossary_db.delete(item.text(), self._current_group)
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
        added = self.glossary_db.import_lines(text.splitlines(), self._current_group)
        self.refresh()
        self.changed.emit()
        InfoBar.success(
            title="导入完成",
            content=f"已写入「{self._current_group}」分组 {added} 条术语。",
            duration=2600,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _export_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出术语表",
            f"术语表-{self._current_group}.txt",
            "文本文件 (*.txt);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(
                self.glossary_db.export_text(self._current_group), encoding="utf-8"
            )
        except OSError as exc:
            self._error(f"写入失败：{exc}")
            return
        InfoBar.success(
            title="导出完成",
            content=f"「{self._current_group}」分组已按「原文→译文」保存。",
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
