"""截屏原位译文贴片：把译文画回原文的位置上。

用法（宿主侧）::

    overlay = TranslationOverlay()
    overlay.show_patches(image, [Patch(bbox, original, translated), ...])
    # Tab / F1 切「译文 / 原文」，Esc 关闭

贴片背景色是从原文边缘采样出来的，所以在浅色纸张、深色终端、彩色海报上
都能贴得比较自然，不会出现刺眼的白块。
"""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PyQt6.QtWidgets import QWidget

from .theme import ACCENT, colors

MIN_FONT = 7
MAX_FONT = 34
PAD = 3


@dataclass
class Patch:
    """一个待贴片的文字块（坐标是原图像素）。"""

    bbox: tuple[int, int, int, int]
    original: str = ""
    translated: str = ""
    score: float = 0.0


def _sample_background(image: QImage, rect: QRect) -> QColor:
    """从文字框外圈采样背景色（避开文字像素本身）。"""
    if image.isNull():
        return QColor(255, 255, 255)
    band = max(2, min(6, rect.height() // 6))
    samples: list[tuple[int, int, int]] = []
    areas = [
        QRect(rect.left(), max(0, rect.top() - band), rect.width(), band),
        QRect(rect.left(), rect.bottom() + 1, rect.width(), band),
        QRect(max(0, rect.left() - band), rect.top(), band, rect.height()),
        QRect(rect.right() + 1, rect.top(), band, rect.height()),
    ]
    for area in areas:
        area = area.intersected(QRect(0, 0, image.width(), image.height()))
        if area.isEmpty():
            continue
        step_x = max(1, area.width() // 8)
        step_y = max(1, area.height() // 3)
        for y in range(area.top(), area.bottom() + 1, step_y):
            for x in range(area.left(), area.right() + 1, step_x):
                color = image.pixelColor(x, y)
                samples.append((color.red(), color.green(), color.blue()))
    if not samples:
        return QColor(255, 255, 255)
    # 中位数比平均值更抗「文字压边」
    reds = sorted(item[0] for item in samples)
    greens = sorted(item[1] for item in samples)
    blues = sorted(item[2] for item in samples)
    mid = len(samples) // 2
    return QColor(reds[mid], greens[mid], blues[mid])


def _ink_for(background: QColor) -> QColor:
    luma = 0.299 * background.red() + 0.587 * background.green() + 0.114 * background.blue()
    return QColor(20, 20, 20) if luma > 140 else QColor(245, 245, 245)


class TranslationOverlay(QWidget):
    """全屏（单屏）只读覆盖层：冻结截图 + 原位译文贴片。"""

    closed = pyqtSignal()
    modeChanged = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._pixmap: QPixmap | None = None
        self._image: QImage | None = None
        self._patches: list[Patch] = []
        self._mode = "translated"  # translated | original
        self._font_scale = 1.0
        self._screen_rect = QRect()

    # ---------------------------------------------------------------- 展示
    @property
    def mode(self) -> str:
        return self._mode

    def show_patches(
        self,
        image: QImage | QPixmap,
        patches: list[Patch],
        *,
        font_scale: float = 1.0,
    ) -> None:
        if isinstance(image, QPixmap):
            self._pixmap = image
            self._image = image.toImage()
        else:
            self._image = image.copy()
            self._pixmap = QPixmap.fromImage(self._image)
        self._patches = [p for p in patches if p.translated or p.original]
        self._font_scale = max(0.6, min(2.0, float(font_scale or 1.0)))
        self._mode = "translated"

        screen = QGuiApplication.screenAt(self._cursor_point()) or QGuiApplication.primaryScreen()
        self._screen_rect = screen.geometry()
        self.setGeometry(self._screen_rect)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self.modeChanged.emit(self._mode)

    @staticmethod
    def _cursor_point() -> QPointF:  # pragma: no cover - 仅用于选屏
        from PyQt6.QtGui import QCursor

        return QCursor.pos()

    def close_overlay(self) -> None:
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def set_mode(self, mode: str) -> None:
        self._mode = "original" if mode == "original" else "translated"
        self.modeChanged.emit(self._mode)
        self.update()

    def toggle_mode(self) -> None:
        self.set_mode("original" if self._mode == "translated" else "translated")

    # ---------------------------------------------------------------- 交互
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_overlay()
            return
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_F1, Qt.Key.Key_Space):
            self.toggle_mode()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.close_overlay()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.toggle_mode()

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(18, 18, 18))
        if self._pixmap is None or self._image is None:
            return

        painter.drawPixmap(self.rect(), self._pixmap)
        if self._mode == "original":
            self._paint_hint(painter, "原文 · 按 Tab 切回译文，Esc 关闭")
            return

        scale_x = self.width() / max(1, self._image.width())
        scale_y = self.height() / max(1, self._image.height())
        for patch in self._patches:
            target = QRectF(
                patch.bbox[0] * scale_x,
                patch.bbox[1] * scale_y,
                (patch.bbox[2] - patch.bbox[0]) * scale_x,
                (patch.bbox[3] - patch.bbox[1]) * scale_y,
            )
            self._paint_patch(painter, patch, target)
        self._paint_hint(painter, f"译文 {len(self._patches)} 处 · Tab 切原文 · Esc 关闭")

    def _paint_patch(self, painter: QPainter, patch: Patch, target: QRectF) -> None:
        if target.width() < 6 or target.height() < 6:
            return
        source_rect = QRect(
            patch.bbox[0],
            patch.bbox[1],
            max(1, patch.bbox[2] - patch.bbox[0]),
            max(1, patch.bbox[3] - patch.bbox[1]),
        )
        background = _sample_background(self._image or QImage(), source_rect)
        ink = _ink_for(background)

        path = QPainterPath()
        radius = min(6.0, target.height() / 3)
        path.addRoundedRect(target.adjusted(-1, -1, 1, 1), radius, radius)
        painter.fillPath(path, background)
        painter.setPen(Qt.PenStyle.NoPen)

        text = patch.translated or patch.original
        if not text:
            return
        font = QFont(self.font())
        font.setBold(True)
        size = self._fit_font(font, text, target)
        if size < MIN_FONT:
            size = MIN_FONT
        font.setPixelSize(max(MIN_FONT, int(size)))
        painter.setFont(font)
        painter.setPen(ink)
        flags = int(
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
            | Qt.TextFlag.TextWordWrap
        )
        painter.drawText(target.adjusted(PAD, PAD, -PAD, -PAD), flags, text)

    def _fit_font(self, font: QFont, text: str, target: QRectF) -> float:
        """从大到小试，找到能放进 bbox 的字号。"""
        available_w = max(6.0, target.width() - PAD * 2)
        available_h = max(6.0, target.height() - PAD * 2)
        size = min(MAX_FONT * self._font_scale, available_h * 1.15)
        while size > MIN_FONT:
            font.setPixelSize(int(size))
            metrics = QFontMetricsF(font)
            lines = _wrap_lines(metrics, text, available_w)
            height = len(lines) * metrics.lineSpacing()
            widest = max((metrics.horizontalAdvance(line) for line in lines), default=0.0)
            if height <= available_h and widest <= available_w:
                return size
            size -= 1.0
        return MIN_FONT

    def _paint_hint(self, painter: QPainter, text: str) -> None:
        font = QFont(self.font())
        font.setPixelSize(13)
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        width = metrics.horizontalAdvance(text) + 28
        rect = QRectF((self.width() - width) / 2, 18, width, 32)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(rect, 16, 16)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(240, 240, 240))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.setPen(QColor(ACCENT))


def _wrap_lines(metrics: QFontMetricsF, text: str, width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if metrics.horizontalAdvance(candidate) > width and current:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def segment_patches(segments: list[dict], translations: list[str] | dict[int, str]) -> list[Patch]:
    """把扩展返回的 segments + 译文列表拼成贴片。"""
    patches: list[Patch] = []
    for index, segment in enumerate(segments):
        bbox = segment.get("bbox") or [0, 0, 0, 0]
        if len(bbox) != 4:
            continue
        if isinstance(translations, dict):
            translated = translations.get(index, "")
        else:
            translated = translations[index] if index < len(translations) else ""
        patches.append(
            Patch(
                bbox=(int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])),
                original=str(segment.get("text") or ""),
                translated=str(translated or ""),
                score=float(segment.get("score") or 0.0),
            )
        )
    return patches