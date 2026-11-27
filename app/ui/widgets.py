"""Custom widgets used by the pages."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    FlowLayout,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    TogglePushButton,
    TransparentToolButton,
)

from ..core.styles import MAX_STYLE_NAME, default_styles, new_style_id
from .theme import MutedLabel, ThemedLabel, colors, on_theme_changed

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


class UrlInputDialog(QDialog):
    """Small themed dialog asking for a remote image URL."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("使用图片链接")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(ThemedLabel("图片链接", size=14, bold=True))
        layout.addWidget(
            MutedLabel("直接透传给模型，请确保链接可公开访问。", size=12)
        )

        self.url_edit = LineEdit()
        self.url_edit.setPlaceholderText("https://example.com/picture.png")
        self.url_edit.setClearButtonEnabled(True)
        layout.addWidget(self.url_edit)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.cancel_button = PushButton("取消")
        self.ok_button = PrimaryPushButton("使用")
        self.ok_button.setMinimumWidth(96)
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        row.addWidget(self.ok_button)
        layout.addLayout(row)

        self.cancel_button.clicked.connect(self.reject)
        self.ok_button.clicked.connect(self.accept)
        self.url_edit.returnPressed.connect(self.accept)

    def url(self) -> str:
        return self.url_edit.text().strip()


class StyleChipBar(QWidget):
    """一排风格预设芯片：点一下就把对应的指令文本填进输入框。"""

    stylePicked = pyqtSignal(str)
    manageRequested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._buttons: dict[str, TogglePushButton] = {}
        self._styles: list[dict[str, str]] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(MutedLabel("风格", size=12))

        self._flow_host = QWidget()
        self._flow = FlowLayout(self._flow_host, needAni=False)
        self._flow.setContentsMargins(0, 0, 0, 0)
        self._flow.setHorizontalSpacing(8)
        self._flow.setVerticalSpacing(6)
        layout.addWidget(self._flow_host, 1)

        self.manage_button = TransparentToolButton(FluentIcon.EDIT)
        self.manage_button.setToolTip("管理风格预设（增删改）")
        self.manage_button.setFixedSize(26, 26)
        self.manage_button.clicked.connect(self.manageRequested.emit)
        layout.addWidget(self.manage_button, 0, Qt.AlignmentFlag.AlignTop)

    # ------------------------------------------------------------------ build
    def set_styles(self, styles: list[dict[str, str]], active_id: str = "") -> None:
        self._styles = list(styles)
        while self._flow.count():
            item = self._flow.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._buttons.clear()

        for style in self._styles:
            button = TogglePushButton(self._flow_host)
            button.setText(style["name"])
            button.setFixedHeight(28)
            button.setToolTip(style["text"] or "不附加任何额外要求")
            button.clicked.connect(lambda _=False, sid=style["id"]: self._pick(sid))
            self._flow.addWidget(button)
            self._buttons[style["id"]] = button
        self.set_active(active_id)

    def set_active(self, active_id: str) -> None:
        """Highlight one chip without emitting anything."""
        for style_id, button in self._buttons.items():
            button.blockSignals(True)
            button.setChecked(style_id == active_id)
            button.blockSignals(False)

    def _pick(self, style_id: str) -> None:
        # TogglePushButton 可以点掉自己，但「当前风格」必须始终有值
        self.set_active(style_id)
        self.stylePicked.emit(style_id)


class StyleManagerDialog(QDialog):
    """增删改风格预设。内置预设也能改文本，但不会因此丢失 id。"""

    def __init__(self, styles: list[dict[str, str]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("风格预设")
        self.setMinimumWidth(640)
        self._rows: list[tuple[str, LineEdit, LineEdit]] = []

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(20, 18, 20, 16)
        self._outer.setSpacing(10)
        self._outer.addWidget(ThemedLabel("风格预设", size=14, bold=True))
        self._outer.addWidget(
            MutedLabel("点击芯片会把这里的文本填进「可选指令」，你可以按需再改。", size=12)
        )

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(8)
        self._outer.addLayout(self._rows_layout)
        for style in styles:
            self._add_row(style["id"], style["name"], style["text"])

        tools = QHBoxLayout()
        tools.setSpacing(10)
        self.add_button = PushButton("添加")
        self.reset_button = PushButton("恢复内置")
        tools.addWidget(self.add_button)
        tools.addWidget(self.reset_button)
        tools.addStretch(1)
        self._outer.addLayout(tools)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.cancel_button = PushButton("取消")
        self.ok_button = PrimaryPushButton("保存")
        self.ok_button.setMinimumWidth(96)
        footer.addStretch(1)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.ok_button)
        self._outer.addLayout(footer)

        self.add_button.clicked.connect(lambda: self._add_row("", "", ""))
        self.reset_button.clicked.connect(self._reset_builtin)
        self.cancel_button.clicked.connect(self.reject)
        self.ok_button.clicked.connect(self.accept)

    def _add_row(self, style_id: str, name: str, text: str) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        name_edit = LineEdit()
        name_edit.setFixedWidth(120)
        name_edit.setPlaceholderText("名称")
        name_edit.setText(name)
        name_edit.setMaxLength(MAX_STYLE_NAME)

        text_edit = LineEdit()
        text_edit.setPlaceholderText("留空 = 不附加任何指令（默认直译）")
        text_edit.setText(text)

        remove = TransparentToolButton(FluentIcon.DELETE)
        remove.setFixedSize(26, 26)
        remove.setToolTip("删除这个风格")

        layout.addWidget(name_edit)
        layout.addWidget(text_edit, 1)
        layout.addWidget(remove)
        self._rows_layout.addWidget(row)
        entry = (style_id, name_edit, text_edit)
        self._rows.append(entry)
        remove.clicked.connect(lambda _=False, r=row, e=entry: self._remove_row(r, e))

    def _remove_row(self, row: QWidget, entry) -> None:
        if entry in self._rows:
            self._rows.remove(entry)
        row.setParent(None)
        row.deleteLater()

    def _reset_builtin(self) -> None:
        for entry in list(self._rows):
            pass
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows.clear()
        for style in default_styles():
            self._add_row(style["id"], style["name"], style["text"])

    def styles(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for style_id, name_edit, text_edit in self._rows:
            name = name_edit.text().strip()
            if not name:
                continue
            sid = style_id or new_style_id([{"id": s, "name": "", "text": ""} for s in seen])
            if sid in seen:
                sid = new_style_id([{"id": s, "name": "", "text": ""} for s in seen])
            seen.add(sid)
            result.append({"id": sid, "name": name[:MAX_STYLE_NAME], "text": text_edit.text().strip()})
        return result or default_styles()


class ImageDropArea(QWidget):
    """Preview surface that also accepts image drops."""

    fileDropped = pyqtSignal(str)
    clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pixmap: QPixmap | None = None
        self._remote = False
        self._caption = "拖入图片，或按 Ctrl+V 粘贴截图"
        self._sub = "支持 PNG / JPG / WEBP / GIF · 点击选择文件"
        self._hover = False
        on_theme_changed(self.update)

    # ------------------------------------------------------------------- state
    def set_image(self, pixmap: QPixmap | None, caption: str = "", sub: str = "") -> None:
        self._pixmap = pixmap
        self._remote = False
        self._caption = caption or self._caption
        self._sub = sub
        self.update()

    def set_remote(self, caption: str, sub: str = "") -> None:
        """Show a remote image (URL) that the model will fetch itself."""
        self._pixmap = None
        self._remote = True
        self._caption = caption
        self._sub = sub or "远程图片链接，翻译时由模型直接读取"
        self.update()

    def clear(self) -> None:
        self._pixmap = None
        self._remote = False
        self._caption = "拖入图片，或按 Ctrl+V 粘贴截图"
        self._sub = "支持 PNG / JPG / WEBP / GIF · 点击选择文件"
        self.update()

    @property
    def empty(self) -> bool:
        return self._pixmap is None

    # ------------------------------------------------------------------ events
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._first_image(event):
            event.acceptProposedAction()
            self._hover = True
            self.update()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def dropEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        path = self._first_image(event)
        if path:
            event.acceptProposedAction()
            self.fileDropped.emit(path)

    @staticmethod
    def _first_image(event) -> str:
        mime = event.mimeData()
        if not mime.hasUrls():
            return ""
        for url in mime.urls():
            if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in IMAGE_SUFFIXES:
                return url.toLocalFile()
        return ""

    # ------------------------------------------------------------------- paint
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = colors()
        rect = QRectF(self.rect()).adjusted(8, 8, -8, -8)

        if self._pixmap is not None and not self._pixmap.isNull():
            painter.setPen(QPen(QColor(c["border"]), 1))
            painter.setBrush(QColor(c["code_bg"]))
            painter.drawRoundedRect(rect, 8, 8)

            inner = rect.adjusted(8, 8, -8, -8)
            scaled = self._pixmap.scaled(
                int(inner.width()),
                int(inner.height()),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = inner.x() + (inner.width() - scaled.width()) / 2
            y = inner.y() + (inner.height() - scaled.height()) / 2
            painter.drawPixmap(int(x), int(y), scaled)

            if self._caption:
                painter.setPen(QColor(c["muted"]))
                font = painter.font()
                font.setPixelSize(12)
                painter.setFont(font)
                painter.drawText(
                    QRectF(rect.left() + 12, rect.bottom() - 24, rect.width() - 24, 18),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    self._caption,
                )
            return

        if self._remote:
            painter.setPen(QPen(QColor(c["accent"]), 1.2))
            painter.setBrush(QColor(c["code_bg"]))
            painter.drawRoundedRect(rect, 8, 8)

            font = painter.font()
            font.setPixelSize(14)
            painter.setFont(font)
            painter.setPen(QColor(c["text"]))
            painter.drawText(
                QRectF(rect.left(), rect.center().y() - 14, rect.width(), 22),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                self._caption,
            )
            font.setPixelSize(11)
            painter.setFont(font)
            painter.setPen(QColor(c["faint"]))
            painter.drawText(
                QRectF(rect.left(), rect.center().y() + 8, rect.width(), 20),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                self._sub,
            )
            return

        pen = QPen(QColor(c["accent"] if self._hover else c["border_strong"]), 1.4)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QColor(c["drop"] if self._hover else c["code_bg"]))
        painter.drawRoundedRect(rect, 10, 10)

        painter.setPen(QColor(c["muted"] if not self._hover else c["accent"]))
        font = painter.font()
        font.setPixelSize(13)
        painter.setFont(font)
        painter.drawText(
            QRectF(rect.left(), rect.center().y() - 14, rect.width(), 22),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            self._caption,
        )
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor(c["faint"]))
        painter.drawText(
            QRectF(rect.left(), rect.center().y() + 8, rect.width(), 20),
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            self._sub,
        )
