"""History list: searchable, paginated, with copy / reload / delete per row."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QGuiApplication, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    SearchLineEdit,
    SmoothScrollArea,
    TitleLabel,
    TransparentToolButton,
)

from ..core.config import model_label
from ..core.history_db import HistoryDB, HistoryEntry
from ..core.languages import display_name
from .theme import Card, MutedLabel, PageWidget, TextLabel, colors

PAGE_SIZE = 50


def _clip(text: str, limit: int) -> str:
    text = (text or "").replace("\r", "").strip()
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class HistoryRow(Card):
    copyRequested = pyqtSignal(object)
    loadRequested = pyqtSignal(object)
    deleteRequested = pyqtSignal(int)

    def __init__(self, entry: HistoryEntry, parent=None) -> None:
        super().__init__(parent, radius=8)
        self.entry = entry

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 11, 12, 11)
        layout.setSpacing(14)

        if entry.image_thumb:
            thumb = QLabel()
            pixmap = QPixmap()
            if pixmap.loadFromData(entry.image_thumb):
                thumb.setPixmap(
                    pixmap.scaled(
                        56,
                        56,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            thumb.setFixedSize(56, 56)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setStyleSheet(
                "border-radius: 6px; background: rgba(128,128,128,0.12);"
                " border: 1px solid rgba(128,128,128,0.22);"
            )
            layout.addWidget(thumb)

        text_box = QVBoxLayout()
        text_box.setSpacing(3)
        text_box.setContentsMargins(0, 0, 0, 0)

        source_label = TextLabel(entry.preview(), size=13, bold=True)
        source_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text_box.addWidget(source_label)

        result_label = MutedLabel(_clip(entry.result_text, 120) or "（无译文）", size=12)
        result_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text_box.addWidget(result_label)

        model = model_label(entry.model) if entry.model else ""
        meta = f"{entry.when} · {display_name(entry.source_lang)} → {display_name(entry.target_lang)}"
        if model:
            meta += f" · {model}"
        if entry.elapsed_ms:
            meta += f" · {entry.elapsed_ms / 1000:.1f}s"
        meta_label = MutedLabel(meta, size=11)
        text_box.addWidget(meta_label)
        layout.addLayout(text_box, 1)

        self.copy_button = TransparentToolButton(FluentIcon.COPY)
        self.copy_button.setToolTip("复制译文")
        self.load_button = TransparentToolButton(FluentIcon.RETURN)
        self.load_button.setToolTip("载入到翻译页")
        self.delete_button = TransparentToolButton(FluentIcon.DELETE)
        self.delete_button.setToolTip("删除这条记录")
        for button in (self.copy_button, self.load_button, self.delete_button):
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.copy_button.clicked.connect(lambda: self.copyRequested.emit(entry))
        self.load_button.clicked.connect(lambda: self.loadRequested.emit(entry))
        self.delete_button.clicked.connect(lambda: self.deleteRequested.emit(entry.id))


class HistoryPage(PageWidget):
    loadRequested = pyqtSignal(object)
    cleared = pyqtSignal()

    def __init__(self, history_db: HistoryDB, parent=None) -> None:
        super().__init__(parent)
        self.history_db = history_db
        self._offset = 0
        self._keyword = ""
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        title = TitleLabel("历史记录")
        self.count_label = MutedLabel("0 条", size=12)
        header.addWidget(title)
        header.addWidget(self.count_label)
        header.addStretch(1)
        self.search = SearchLineEdit()
        self.search.setPlaceholderText("搜索原文或译文")
        self.search.setMinimumWidth(260)
        self.clear_button = PushButton("清空全部")
        header.addWidget(self.search)
        header.addWidget(self.clear_button)
        outer.addLayout(header)

        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(SmoothScrollArea.Shape.NoFrame)
        self.scroll.setStyleSheet("SmoothScrollArea { background: transparent; }")

        self.container = QWidget()
        self.container.setStyleSheet("background: transparent;")
        self.rows_layout = QVBoxLayout(self.container)
        self.rows_layout.setContentsMargins(0, 0, 6, 0)
        self.rows_layout.setSpacing(8)
        self.scroll.setWidget(self.container)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll, 1)

        self.empty_label = MutedLabel("还没有翻译记录，去「翻译」页试试吧。", size=13)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setParent(self.container)
        self.empty_label.hide()

        self.more_button = PrimaryPushButton("加载更多")
        self.more_button.setVisible(False)
        more_row = QHBoxLayout()
        more_row.addStretch(1)
        more_row.addWidget(self.more_button)
        more_row.addStretch(1)
        outer.addLayout(more_row)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self._on_search)

        self.search.textChanged.connect(lambda _: self._debounce.start())
        self.more_button.clicked.connect(self._load_more)
        self.clear_button.clicked.connect(self._clear_all)

    # ------------------------------------------------------------------ events
    def _on_search(self) -> None:
        self._keyword = self.search.text().strip()
        self._offset = 0
        self.refresh()

    def _load_more(self) -> None:
        self._offset += PAGE_SIZE
        self._render(reset=False)

    def refresh(self) -> None:
        self._offset = 0
        self._render(reset=True)

    def _reset_layout(self) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.empty_label:
                widget.deleteLater()
        self.empty_label.hide()

    def _render(self, *, reset: bool) -> None:
        if reset:
            self._reset_layout()

        entries = self.history_db.search(self._keyword, limit=PAGE_SIZE, offset=self._offset)
        total = self.history_db.count(self._keyword)
        self.count_label.setText(f"{total} 条")

        if total == 0:
            self.empty_label.setText(
                f"没有匹配「{self._keyword}」的记录。"
                if self._keyword
                else "还没有翻译记录，去「翻译」页试试吧。"
            )
            self.rows_layout.addWidget(self.empty_label)
            self.empty_label.show()
            self.more_button.setVisible(False)
            return

        for entry in entries:
            row = HistoryRow(entry, self.container)
            row.copyRequested.connect(self._copy_entry)
            row.loadRequested.connect(self.loadRequested.emit)
            row.deleteRequested.connect(self._delete_entry)
            self.rows_layout.addWidget(row)

        self.rows_layout.addStretch(1)
        self.more_button.setVisible(self._offset + len(entries) < total)

    # ----------------------------------------------------------------- actions
    def _copy_entry(self, entry: HistoryEntry) -> None:
        if not (entry.result_text or "").strip():
            return
        QGuiApplication.clipboard().setText(entry.result_text)
        InfoBar.success(
            title="已复制",
            content="这条译文已复制到剪贴板。",
            duration=1600,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _delete_entry(self, entry_id: int) -> None:
        self.history_db.delete(entry_id)
        self._offset = 0
        self.refresh()

    def _clear_all(self) -> None:
        from qfluentwidgets import MessageBox

        box = MessageBox("清空历史记录", "将删除本机保存的全部翻译记录，此操作不可撤销。", self.window())
        if not box.exec():
            return
        self.history_db.clear()
        self._offset = 0
        self.refresh()
        self.cleared.emit()
