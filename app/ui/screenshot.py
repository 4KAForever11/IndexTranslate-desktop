"""框选截屏：全屏半透明遮罩 + 拖拽选区，截完直接进图片翻译管线。

为什么自己做而不是调用系统截图工具：
* 系统工具（Snipping Tool / Win+Shift+S）需要先保存或复制，再切回本程序粘贴；
* 这里选区一松手就拿到 ``QImage``，可以立刻送进 ``image_utils`` 的压缩 + Base64 流程，
  少两次剪贴板往返，也不会被其他程序覆盖剪贴板。

已知取舍：只在鼠标所在的那块屏幕上框选（多屏时先移鼠标到目标屏）。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QWidget

from .theme import ACCENT

MIN_SELECTION = 6  # 逻辑像素，太小的选区视为误触
_HINT = "拖动鼠标框选要翻译的区域 · Esc 取消"


class CaptureOverlay(QWidget):
    """覆盖整屏的选区遮罩。拖拽结束发 ``captured``，取消发 ``cancelled``。"""

    captured = pyqtSignal(QImage)
    cancelled = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self._screen = None
        self._pixmap = QPixmap()
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------ 启动
    def start(self) -> None:
        """先截屏再显示遮罩，避免把遮罩本身拍进去。"""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            self.cancelled.emit()
            return
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            self.cancelled.emit()
            return
        self._screen = screen
        self._pixmap = pixmap
        self.setGeometry(screen.geometry())
        self._origin = None
        self._current = None
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    # ------------------------------------------------------------------ 绘制
    def _selection(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        return QRect(self._origin, self._current).normalized()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self._pixmap.isNull():
            painter.drawPixmap(0, 0, self._pixmap)

        selection = self._selection()
        if selection.isNull() or selection.width() < 1 or selection.height() < 1:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
            self._draw_hint(painter)
            return

        # 选区外压暗：外框 + 内框走奇偶填充，只填充中间那圈。
        path = QPainterPath()
        path.addRect(QRectF(self.rect()))
        path.addRect(QRectF(selection))
        path.setFillRule(Qt.FillRule.OddEvenFill)
        painter.fillPath(path, QColor(0, 0, 0, 120))

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(ACCENT), 1))
        painter.drawRect(selection)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ACCENT))
        for corner in (
            selection.topLeft(),
            selection.topRight(),
            selection.bottomLeft(),
            selection.bottomRight(),
        ):
            painter.drawRect(QRect(corner.x() - 2, corner.y() - 2, 4, 4))

        self._draw_size(painter, selection)

    def _draw_hint(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(_HINT) + 28
        height = metrics.height() + 16
        box = QRect(
            (self.width() - width) // 2,
            max(24, self.height() // 12),
            width,
            height,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(box, height / 2, height / 2)
        painter.setPen(QColor(255, 255, 255, 235))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, _HINT)

    def _draw_size(self, painter: QPainter, selection: QRect) -> None:
        label = f"{selection.width()} × {selection.height()}"
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(label) + 14
        height = metrics.height() + 8
        x = min(max(0, selection.left()), self.width() - width)
        y = selection.top() - height - 6
        if y < 0:
            y = min(self.height() - height, selection.bottom() + 6)
        box = QRect(int(x), int(y), width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 190))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    # ------------------------------------------------------------------ 交互
    @staticmethod
    def _clamp(point: QPoint, rect: QRect) -> QPoint:
        return QPoint(
            min(max(point.x(), rect.left()), rect.right()),
            min(max(point.y(), rect.top()), rect.bottom()),
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self._cancel()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position = self._clamp(event.position().toPoint(), self.rect())
        self._origin = position
        self._current = position
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._origin is None:
            return
        self._current = self._clamp(event.position().toPoint(), self.rect())
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._origin is None or event.button() != Qt.MouseButton.LeftButton:
            return
        self._current = self._clamp(event.position().toPoint(), self.rect())
        selection = self._selection()
        self._origin = None
        self._current = None
        if selection.width() < MIN_SELECTION or selection.height() < MIN_SELECTION:
            self._cancel()
            return
        self._emit(selection)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ 收尾
    def _emit(self, selection: QRect) -> None:
        ratio = self._pixmap.devicePixelRatio() or 1.0
        source = QRect(
            int(round(selection.x() * ratio)),
            int(round(selection.y() * ratio)),
            max(1, int(round(selection.width() * ratio))),
            max(1, int(round(selection.height() * ratio))),
        )
        image = self._pixmap.copy(source).toImage()
        self.close()
        if image.isNull():
            self.cancelled.emit()
            return
        self.captured.emit(image)

    def _cancel(self) -> None:
        self.close()
        self.cancelled.emit()


def capture_region(on_captured, on_cancelled=None) -> CaptureOverlay:
    """便捷入口：接好信号并启动遮罩，返回遮罩对象（需自己持有引用）。"""
    overlay = CaptureOverlay()
    overlay.captured.connect(on_captured)
    if on_cancelled is not None:
        overlay.cancelled.connect(on_cancelled)
    overlay.start()
    return overlay
