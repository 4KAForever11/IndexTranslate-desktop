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
from qfluentwidgets import LineEdit, PrimaryPushButton, PushButton

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
