"""无边框流式划词浮窗。

设计目标：轻、快、不挡视线。

* ``Tool`` 窗口标志 —— 不会在任务栏里多出一个图标，也不抢焦点。
* **失焦即隐藏**（``WindowDeactivate``）并取消未完成的请求；右上角图钉可以锁定常驻。
* 出现在光标右下方 14px，用 ``screenAt(pos).availableGeometry()`` 钳制，贴边时翻到另一侧，
  不会露到屏幕外。
* 正文流式渲染，底部小字提示「缓存命中」「已降级至 9B」——这些信息来自
  ``TranslationPipeline`` 的分片，不是界面自己猜的。
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPoint, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, InfoBar, InfoBarPosition, PushButton, TransparentToolButton

from ..core import settings
from ..core.config import model_label
from ..core.languages import display_name
from ..workers.translation_worker import TranslationWorker, build_request
from .theme import Divider, MutedLabel, StatusDot, TextLabel, colors, on_theme_changed

MAX_OUTPUT_HEIGHT = 300
MIN_OUTPUT_HEIGHT = 44
POPUP_WIDTH = 460
CURSOR_OFFSET = 14

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
    pinnedChanged = pyqtSignal(bool)

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
        self._notice = ""
        self._pinned = bool(settings.get("popup_pinned", False))
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
        self._model_label = MutedLabel("", size=11)
        header.addWidget(self._title)
        header.addWidget(self._lang_label)
        header.addStretch(1)
        header.addWidget(self._model_label)

        self.pin_button = TransparentToolButton(FluentIcon.PIN)
        self.pin_button.setFixedSize(24, 24)
        self.pin_button.setCheckable(True)
        self.pin_button.setChecked(self._pinned)
        self.pin_button.setToolTip("图钉：锁定后点击外部不会关闭")
        self.pin_button.toggled.connect(self._on_pin_toggled)
        header.addWidget(self.pin_button)

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

        self._refresh_pin_icon()
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

    # -------------------------------------------------------------------- 图钉
    def _refresh_pin_icon(self) -> None:
        self.pin_button.setIcon(FluentIcon.UNPIN if self._pinned else FluentIcon.PIN)
        self.pin_button.setToolTip(
            "已锁定：点击外部不会关闭" if self._pinned else "图钉：锁定后点击外部不会关闭"
        )

    def _on_pin_toggled(self, checked: bool) -> None:
        self._pinned = bool(checked)
        self._refresh_pin_icon()
        settings.set("popup_pinned", self._pinned)
        settings.save()
        self.pinnedChanged.emit(self._pinned)

    @property
    def pinned(self) -> bool:
        return self._pinned

    # ------------------------------------------------------------------ show
    def show_for(self, text: str, *, target_lang: str | None = None) -> None:
        """弹出并开始翻译。``text`` 为空时只提示，不发请求。"""
        text = (text or "").strip()
        self.stop()
        self._source_text = text
        self._notice = ""
        self._model_label.setText("")
        self.output.clear()
        self.copy_button.setEnabled(False)

        if not text:
            self.source_label.setText("")
            self._set_status("没有取到选中的文字", colors()["warn"])
            self._place_near_cursor()
            self._show()
            return

        self.source_label.setText(text if len(text) <= 120 else text[:120] + "…")
        code = str(target_lang or settings.get("target_lang", "zh") or "zh")
        self._lang_label.setText(f"· {display_name(code)}")
        self._set_status("正在翻译…", colors()["accent"])
        self._fit_output("正在翻译…")
        self._place_near_cursor()
        self._show()
        self._start_worker(text, code)

    def _show(self) -> None:
        self.show()
        self.raise_()
        # 不抢焦点：抢焦点会让目标程序失去选区高亮，也容易触发失焦隐藏。
        if self._pinned:
            self.activateWindow()

    def show_image(self, image, *, target_lang: str | None = None) -> None:
        """框选截屏的结果直接进浮窗：不切窗口，也不经过剪贴板。"""
        self.stop()
        self._source_text = ""
        self._notice = ""
        self._model_label.setText("")
        self.output.clear()
        self.copy_button.setEnabled(False)

        code = str(target_lang or settings.get("target_lang", "zh") or "zh")
        self.source_label.setText("（框选图片）")
        self._lang_label.setText(f"· {display_name(code)}")
        self._set_status("正在识别图片…", colors()["accent"])
        self._fit_output("正在识别图片…")
        self._place_near_cursor()
        self._show()

        request = build_request(image_qimage=image, kind="image", target_lang=code, scene="popup")
        worker = TranslationWorker(request, self)
        worker.delta.connect(self._on_delta)
        worker.notice.connect(self._on_notice)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.stopped.connect(self._on_stopped)
        worker.finished.connect(self._on_finished)
        self._worker = worker
        self._buffer.clear()
        worker.start()

    def _start_worker(self, text: str, code: str) -> None:
        glossary = ""
        if bool(settings.get("use_glossary", True)) and self._glossary_provider is not None:
            try:
                glossary = self._glossary_provider.prompt_for(text)
            except Exception:
                glossary = ""

        request = build_request(text=text, glossary=glossary, target_lang=code, scene="popup")
        worker = TranslationWorker(request, self)
        worker.delta.connect(self._on_delta)
        worker.notice.connect(self._on_notice)
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

    def _on_notice(self, text: str) -> None:
        """管道给的提示：缓存命中 / 已自动降级。"""
        self._notice = text
        if text.startswith("已自动降级"):
            self._model_label.setText(text.replace("已自动降级至 ", ""))
        self._set_status(self._status_text(), colors()["muted"] if text == "缓存命中" else colors()["warn"])

    def _status_text(self) -> str:
        base = "正在翻译…" if self._worker is not None else ""
        if self._notice and base:
            return f"{base} · {self._notice}"
        return self._notice or base

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
        suffix = f" · {self._notice}" if self._notice else ""
        self._set_status(f"完成 · {elapsed_ms / 1000:.1f}s{suffix}", colors()["ok"])

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
        """出现在鼠标右下方；贴近屏幕边缘时翻到另一侧，绝不露到屏幕外。"""
        self.adjustSize()
        anchor = QCursor.pos()
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        width, height = self.width(), self.height()
        x = anchor.x() + CURSOR_OFFSET
        y = anchor.y() + CURSOR_OFFSET + 6
        if x + width > area.right():
            x = anchor.x() - width - CURSOR_OFFSET
        if y + height > area.bottom():
            y = anchor.y() - height - CURSOR_OFFSET
        x = max(area.left(), min(x, area.right() - width))
        y = max(area.top(), min(y, area.bottom() - height))
        self.move(QPoint(int(x), int(y)))

    # ------------------------------------------------------------------ events
    def event(self, event) -> bool:  # noqa: N802
        # 失焦自动隐藏：点外部就走，但被图钉锁定时保持常驻。
        if event.type() == QEvent.Type.WindowDeactivate and not self._pinned:
            if self.isVisible():
                self.dismiss()
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self.stop()
        super().hideEvent(event)


# 旧名字保留，避免外部引用（托盘 / 主窗口）因为改名而断掉。
__all__ = ["TranslatePopup", "MAX_OUTPUT_HEIGHT", "MIN_OUTPUT_HEIGHT", "POPUP_WIDTH"]
