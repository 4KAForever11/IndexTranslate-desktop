"""内置高 DPI 多屏框选截屏。

多显示器混合缩放（主屏 150% + 副屏 100%）是截屏功能最容易画糊、偏移的地方。
这里采用「按光标所在屏幕独立铺满遮罩」的做法，而不是硬拼一整张虚拟桌面大图：

1. 取光标所在屏幕 → ``screen.grabWindow(0)`` 冻结当前画面，同时记住 ``devicePixelRatio``；
2. 遮罩几何设为 ``screen.geometry()``，``paintEvent`` 先铺冻结底图再压暗，选区挖空；
3. 松手时把逻辑选区乘上 ``dpr`` 再裁剪——高分屏裁出来的是物理原生像素，不会糊；
4. 结果直接进 ``image_utils`` 的压缩 + Base64，不需要经过系统截图工具和剪贴板。

为什么自己做而不是调系统截图（Win+Shift+S）：系统工具要落盘或进剪贴板，再切回本
程序粘贴；这里松手就拿到 ``QImage``，少两次往返，也不会覆盖用户剪贴板。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
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
    def start(self, screen=None) -> None:
        """先截屏再显示遮罩，避免把遮罩本身拍进去。

        ``screen`` 为空时用光标所在屏幕；多屏场景可以先移动鼠标再按热键。
        """
        screen = screen or QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
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

    @property
    def device_pixel_ratio(self) -> float:
        return float(self._pixmap.devicePixelRatio() or 1.0)

    # ------------------------------------------------------------------ 绘制
    def _selection(self) -> QRect:
        """拖拽出来的选区，按半开区间算（拖 400 px 就是 400 px，不是 401）。"""
        if self._origin is None or self._current is None:
            return QRect()
        x1, x2 = sorted((self._origin.x(), self._current.x()))
        y1, y2 = sorted((self._origin.y(), self._current.y()))
        return QRect(x1, y1, x2 - x1, y2 - y1)

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
        ratio = self.device_pixel_ratio
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


def capture_region(on_captured, on_cancelled=None, screen=None) -> CaptureOverlay:
    """便捷入口：接好信号并启动遮罩，返回遮罩对象（需自己持有引用）。"""
    overlay = CaptureOverlay()
    overlay.captured.connect(on_captured)
    if on_cancelled is not None:
        overlay.cancelled.connect(on_cancelled)
    overlay.start(screen)
    return overlay
