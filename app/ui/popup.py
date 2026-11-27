"""划词浮窗：在鼠标旁弹出一个轻量无边框小窗，流式显示译文。

设计取舍：
* 无边框 + 半透明 + 圆角 + 投影，视觉上像系统原生的取词气泡。
* 点浮窗以外的区域不关闭（避免误触），用 Esc 或右上角关闭按钮退出。
* 复制按钮、转到主窗口都在浮窗内完成，用户可以完全不回主窗口。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, InfoBar, InfoBarPosition, PushButton, TransparentToolButton

from ..core import settings
from ..core.languages import display_name
from ..workers.translation_worker import TranslationWorker, build_request
from .theme import Divider, MutedLabel, StatusDot, TextLabel, colors, on_theme_changed

MAX_OUTPUT_HEIGHT = 300
MIN_OUTPUT_HEIGHT = 44
POPUP_WIDTH = 460

_BROWSER_QSS = """
QTextBrowser {
    background: transparent;
    border: none;
    padding: 0px;
    color: %(text)s;
    selection-background-color: %(accent)s;
}
QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: %(scroll)s;
    border-radius: 3px;
    min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""


class _Surface(QFrame):
    """圆角卡片背景（自绘，跟随主题）。"""

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = colors()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(c["border"]), 1))
        painter.setBrush(QColor(c["card"]))
        painter.drawRoundedRect(rect, 12, 12)


class TranslatePopup(QWidget):
    """划词翻译浮窗。同一时间只跑一个翻译任务。"""

    openMainRequested = pyqtSignal(str)

    def __init__(self, glossary_provider=None, parent=None) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self._glossary_provider = glossary_provider
        self._worker: TranslationWorker | None = None
        self._buffer: list[str] = []
        self._source_text = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(POPUP_WIDTH + 24)
        self._build_ui()
        on_theme_changed(self.apply_theme)

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        self._surface = _Surface(self)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 90))
        self._surface.setGraphicsEffect(shadow)
        outer.addWidget(self._surface)

        body = QVBoxLayout(self._surface)
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = TextLabel("划词翻译", size=12, bold=True)
        self._lang_label = MutedLabel("", size=11)
        header.addWidget(self._title)
        header.addWidget(self._lang_label)
        header.addStretch(1)
        self.close_button = TransparentToolButton(FluentIcon.CLOSE)
        self.close_button.setFixedSize(24, 24)
        self.close_button.setToolTip("关闭（Esc）")
        self.close_button.clicked.connect(self.dismiss)
        header.addWidget(self.close_button)
        body.addLayout(header)

        self.source_label = MutedLabel("", size=11)
        self.source_label.setWordWrap(True)
        self.source_label.setMaximumHeight(38)
        body.addWidget(self.source_label)

        body.addWidget(Divider())

        self.output = QTextBrowser()
        self.output.setReadOnly(True)
        self.output.setFrameShape(QFrame.Shape.NoFrame)
        self.output.document().setDocumentMargin(0)
        self.output.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.output.setMinimumHeight(MIN_OUTPUT_HEIGHT)
        body.addWidget(self.output)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.status_dot = StatusDot()
        self.status_label = MutedLabel("正在翻译…", size=11)
        footer.addWidget(self.status_dot)
        footer.addWidget(self.status_label, 1)

        self.copy_button = PushButton("复制")
        self.copy_button.setFixedHeight(28)
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_result)
        footer.addWidget(self.copy_button)

        self.open_button = PushButton("转到主窗口")
        self.open_button.setFixedHeight(28)
        self.open_button.clicked.connect(self._open_main)
        footer.addWidget(self.open_button)
        body.addLayout(footer)

        self.apply_theme()

    def apply_theme(self) -> None:
        c = colors()
        self.output.setStyleSheet(
            _BROWSER_QSS
            % {
                "text": c["text"],
                "accent": c["accent_soft"],
                "scroll": c["border_strong"],
            }
        )
        self._surface.update()
        self.update()

    # ------------------------------------------------------------------ show
    def show_for(self, text: str, *, target_lang: str | None = None) -> None:
        """弹出并开始翻译。``text`` 为空时只提示，不发请求。"""
        text = (text or "").strip()
        self.stop()
        self._source_text = text
        self.output.clear()
        self.copy_button.setEnabled(False)

        if not text:
            self.source_label.setText("")
            self._set_status("没有取到选中的文字", colors()["warn"])
            self._place_near_cursor()
            self.show()
            self.raise_()
            self.activateWindow()
            return

        self.source_label.setText(text if len(text) <= 120 else text[:120] + "…")
        code = str(target_lang or settings.get("target_lang", "zh") or "zh")
        self._lang_label.setText(f"· {display_name(code)}")
        self._set_status("正在翻译…", colors()["accent"])
        self._fit_output("正在翻译…")
        self._place_near_cursor()
        self.show()
        self.raise_()
        self.activateWindow()
        self._start_worker(text, code)

    def _start_worker(self, text: str, code: str) -> None:
        glossary = ""
        if bool(settings.get("use_glossary", True)) and self._glossary_provider is not None:
            try:
                glossary = self._glossary_provider.prompt_for(text)
            except Exception:
                glossary = ""

        request = build_request(text=text, glossary=glossary, target_lang=code)
        worker = TranslationWorker(request, self)
        worker.delta.connect(self._on_delta)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.stopped.connect(self._on_stopped)
        worker.finished.connect(self._on_finished)
        self._worker = worker
        self._buffer.clear()
        worker.start()

    # --------------------------------------------------------------- streaming
    def _on_delta(self, chunk: str) -> None:
        self._buffer.append(chunk)
        self._render("".join(self._buffer))

    def _render(self, text: str) -> None:
        self._fit_output(text)
        at_bottom = self.output.verticalScrollBar().value() >= (
            self.output.verticalScrollBar().maximum() - 4
        )
        self.output.setPlainText(text)
        if at_bottom:
            bar = self.output.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _fit_output(self, text: str) -> None:
        """按内容行数自适应高度，超过上限就滚动。"""
        self.output.setFixedHeight(MIN_OUTPUT_HEIGHT)
        document = self.output.document()
        document.setTextWidth(self.output.viewport().width() or (POPUP_WIDTH - 40))
        needed = int(document.size().height()) + 10
        height = max(MIN_OUTPUT_HEIGHT, min(MAX_OUTPUT_HEIGHT, needed))
        self.output.setFixedHeight(height)
        self.adjustSize()

    def _on_done(self, text: str, elapsed_ms: int) -> None:
        self._render(text)
        self.copy_button.setEnabled(bool(text.strip()))
        self._set_status(f"完成 · {elapsed_ms / 1000:.1f}s", colors()["ok"])

    def _on_failed(self, message: str, detail: str) -> None:
        self._fit_output(message)
        self.output.setPlainText(message)
        self._set_status("翻译失败", colors()["danger"])
        self.setToolTip(detail[:600] if detail else "")

    def _on_stopped(self, partial: str) -> None:
        if partial:
            self._render(partial)
        self._set_status("已停止", colors()["muted"])

    def _on_finished(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.setText(text)
        self.status_dot.set_color(color)

    # ------------------------------------------------------------------ action
    def copy_result(self) -> None:
        text = self.output.toPlainText().strip()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        InfoBar.success(
            title="已复制",
            content="译文已放入剪贴板。",
            duration=1600,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _open_main(self) -> None:
        text = self._source_text
        self.dismiss()
        if text:
            self.openMainRequested.emit(text)

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    def dismiss(self) -> None:
        self.stop()
        self.hide()

    # ------------------------------------------------------------------ layout
    def _place_near_cursor(self) -> None:
        """出现在鼠标右下角；贴近屏幕边缘时自动翻到另一侧。"""
        self.adjustSize()
        anchor = QCursor.pos()
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        width, height = self.width(), self.height()
        x = anchor.x() + 16
        y = anchor.y() + 20
        if x + width > area.right():
            x = max(area.left(), anchor.x() - width - 16)
        if y + height > area.bottom():
            y = max(area.top(), anchor.y() - height - 16)
        self.move(QPoint(x, y))

    # ------------------------------------------------------------------ events
    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self.stop()
        super().hideEvent(event)
