"""Colours, painted containers and small theme-aware primitives.

The fluent stylesheets are compiled into Qt resources, so a window-level
stylesheet cannot reliably restyle their widgets. Instead of fighting that,
everything custom here paints itself with ``QPainter`` and re-reads the palette
whenever ``qconfig.themeChanged`` fires.
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PyQt6.QtWidgets import QFrame, QLabel, QSizePolicy, QWidget
from qfluentwidgets import isDarkTheme, qconfig

ACCENT = "#0078D4"
ACCENT_HOVER = "#106EBE"

LIGHT = {
    "card": "#FFFFFF",
    "card_alt": "#FAFAFA",
    "border": "#E4E6EB",
    "border_strong": "#D3D6DC",
    "text": "#1B1B1F",
    "muted": "#71737A",
    "faint": "#9A9CA3",
    "window": "#F3F4F6",
    "accent": ACCENT,
    "accent_soft": "#E5F1FB",
    "ok": "#107C10",
    "warn": "#B26A00",
    "danger": "#C42B1C",
    "drop": "#EDEFF3",
    "code_bg": "#F6F7F9",
}

DARK = {
    "card": "#2B2B2E",
    "card_alt": "#313135",
    "border": "#3C3C41",
    "border_strong": "#4A4A50",
    "text": "#F2F2F5",
    "muted": "#A4A6AD",
    "faint": "#85878E",
    "window": "#202023",
    "accent": "#4CA0E8",
    "accent_soft": "#1E3A52",
    "ok": "#54B054",
    "warn": "#E0A030",
    "danger": "#F1707B",
    "drop": "#2F2F33",
    "code_bg": "#26262A",
}


def is_dark() -> bool:
    """Fluent tracks the theme itself; the Qt palette is NOT updated for dark mode."""
    try:
        return bool(isDarkTheme())
    except Exception:
        return False


def colors() -> dict[str, str]:
    return DARK if is_dark() else LIGHT


_LISTENERS: list[Callable[[], None]] = []
_connected = False


def _dispatch() -> None:
    for callback in list(_LISTENERS):
        try:
            callback()
        except Exception:
            pass


def on_theme_changed(callback: Callable[[], None]) -> None:
    """Register a repaint hook. Prefer this over touching ``qconfig`` directly."""
    global _connected
    _LISTENERS.append(callback)
    if _connected:
        return
    _connected = True
    # themeChanged fires before the new stylesheet is applied, themeChangedFinished
    # afterwards; listening to both keeps custom painting in sync in either case.
    for name in ("themeChanged", "themeChangedFinished"):
        signal = getattr(qconfig, name, None)
        if signal is None:
            continue
        try:
            signal.connect(lambda *_: _dispatch())
        except Exception:
            pass


def refresh_theme() -> None:
    """Force every registered widget to re-read the colours and repaint."""
    _dispatch()


class PageWidget(QWidget):
    """Base page that paints the themed window background behind its cards."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        on_theme_changed(self.update)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(colors()["window"]))


class Card(QFrame):
    """A rounded, themed surface. Children are added normally."""

    def __init__(self, parent=None, *, radius: int = 10, padding: int = 0) -> None:
        super().__init__(parent)
        self._radius = radius
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        if padding:
            self.setContentsMargins(padding, padding, padding, padding)
        on_theme_changed(self.update)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = colors()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(c["border"]), 1))
        painter.setBrush(QColor(c["card"]))
        painter.drawRoundedRect(rect, self._radius, self._radius)


class ThemedLabel(QLabel):
    """Plain QLabel keeps the light-theme text colour, so colour it ourselves."""

    def __init__(
        self,
        text: str = "",
        parent=None,
        *,
        size: int = 12,
        bold: bool = False,
        role: str = "text",
    ) -> None:
        super().__init__(text, parent)
        font = self.font()
        font.setPixelSize(size)
        if bold:
            font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self._role = role
        self._apply_color()
        on_theme_changed(self._apply_color)

    def _apply_color(self) -> None:
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.WindowText, QColor(colors()[self._role]))
        self.setPalette(palette)


class MutedLabel(ThemedLabel):
    """Secondary text; fluent's own labels are not muted enough."""

    def __init__(self, text: str = "", parent=None, *, size: int = 12, bold: bool = False) -> None:
        super().__init__(text, parent, size=size, bold=bold, role="muted")


class TextLabel(ThemedLabel):
    """Primary text as a plain label (used where fluent labels would be too heavy)."""

    def __init__(self, text: str = "", parent=None, *, size: int = 13, bold: bool = False) -> None:
        super().__init__(text, parent, size=size, bold=bold, role="text")


class Divider(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        on_theme_changed(self.update)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(colors()["border"]))


class StatusDot(QWidget):
    """A 8 px dot used next to the status message."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._color = ACCENT
        on_theme_changed(self.update)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._color))
        painter.drawEllipse(QRectF(1, 1, 8, 8))


class AccentBar(QWidget):
    """A 3 px accent strip used on drop areas and active cards."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(3)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        on_theme_changed(self.update)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(colors()["accent"]))
