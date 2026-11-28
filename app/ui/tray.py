"""系统托盘常驻图标。

关掉主窗口后程序继续活在托盘里，本地代理、全局热键、剪贴板监听才不会断。
菜单只负责发出请求，真正的动作由 ``MainWindow`` 处理。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QSystemTrayIcon

from qfluentwidgets import Action, RoundMenu

from ..core.config import APP_NAME, resource_path
from .theme import ACCENT


def app_icon() -> QIcon:
    """窗口/托盘图标；打包漏掉资源时临时画一个，不让程序崩在启动阶段。"""
    path = resource_path("assets", "icon.ico")
    try:
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    except OSError:
        pass

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ACCENT))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    font = QFont("Microsoft YaHei", 26)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "译")
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    """托盘图标 + 右键菜单。"""

    showRequested = pyqtSignal()
    quitRequested = pyqtSignal()
    proxyToggleRequested = pyqtSignal()
    popupRequested = pyqtSignal()
    screenshotRequested = pyqtSignal()
    replaceRequested = pyqtSignal()
    subtitleRequested = pyqtSignal()
    extensionsRequested = pyqtSignal()
    githubRequested = pyqtSignal()
    minimizeToTrayChanged = pyqtSignal(bool)
    autostartChanged = pyqtSignal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setIcon(app_icon())
        self.setToolTip(APP_NAME)

        self._menu = RoundMenu(parent=None)
        self._build_menu()
        # 不用 setContextMenu：RoundMenu 是自绘的 QWidget 菜单，交给系统按光标位置弹时
        # 不会自己翻到屏幕内侧，托盘在右下角时最下面的「退出」就被裁掉了。
        self.activated.connect(self._on_activated)

    # -------------------------------------------------------------------- menu
    def _build_menu(self) -> None:
        self._menu.setMinimumWidth(236)

        self.open_action = Action("打开主窗口", parent=self._menu)
        self.open_action.triggered.connect(self.showRequested.emit)
        self._menu.addAction(self.open_action)

        # 划词/截屏/替换收进子菜单：托盘菜单一旦超过屏幕高度，
        # 最下面的「退出」就被挤出去了（Win10 上特别常见）。
        self.tools_menu = RoundMenu("划词与截屏", parent=self._menu)
        self.popup_action = Action("划词翻译", parent=self.tools_menu)
        self.popup_action.triggered.connect(self.popupRequested.emit)
        self.tools_menu.addAction(self.popup_action)

        self.shot_action = Action("截屏翻译", parent=self.tools_menu)
        self.shot_action.triggered.connect(self.screenshotRequested.emit)
        self.tools_menu.addAction(self.shot_action)

        self.replace_action = Action("输入框替换", parent=self.tools_menu)
        self.replace_action.triggered.connect(self.replaceRequested.emit)
        self.tools_menu.addAction(self.replace_action)
        self._menu.addMenu(self.tools_menu)

        self.subtitle_action = Action("实时字幕（系统音频）", parent=self._menu, checkable=True)
        self.subtitle_action.triggered.connect(self.subtitleRequested.emit)
        self._menu.addAction(self.subtitle_action)

        extensions_action = Action("扩展中心…", parent=self._menu)
        extensions_action.triggered.connect(self.extensionsRequested.emit)
        self._menu.addAction(extensions_action)

        # 托盘里也留官方入口：改包伪冒最常见的入口就是「同名软件的另一份 exe」。
        self.github_action = Action("官方 GitHub 仓库…", parent=self._menu)
        self.github_action.triggered.connect(self.githubRequested.emit)
        self._menu.addAction(self.github_action)
        self._menu.addSeparator()

        self.proxy_action = Action("本地代理：未启动", parent=self._menu)
        self.proxy_action.triggered.connect(self.proxyToggleRequested.emit)
        self._menu.addAction(self.proxy_action)

        self.tray_action = Action("关闭后最小化到托盘", parent=self._menu, checkable=True, checked=True)
        self.tray_action.triggered.connect(
            lambda checked: self.minimizeToTrayChanged.emit(bool(checked))
        )
        self._menu.addAction(self.tray_action)

        self.autostart_action = Action("开机静默自启", parent=self._menu, checkable=True, checked=False)
        self.autostart_action.triggered.connect(
            lambda checked: self.autostartChanged.emit(bool(checked))
        )
        self._menu.addAction(self.autostart_action)

        self._menu.addSeparator()
        self.quit_action = Action("退出", parent=self._menu)
        self.quit_action.triggered.connect(self.quitRequested.emit)
        self._menu.addAction(self.quit_action)

    # ------------------------------------------------------------------ update
    def set_proxy_running(self, running: bool, url: str = "") -> None:
        self.proxy_action.setText(
            f"本地代理：运行中 {url}" if running and url else "本地代理：未启动"
        )

    def set_popup_hotkey(self, sequence: str) -> None:
        self.popup_action.setText(f"划词翻译  {sequence}" if sequence else "划词翻译")

    def set_shot_hotkey(self, sequence: str) -> None:
        self.shot_action.setText(f"截屏翻译  {sequence}" if sequence else "截屏翻译")

    def set_replace_hotkey(self, sequence: str) -> None:
        self.replace_action.setText(f"输入框替换  {sequence}" if sequence else "输入框替换")

    def set_state(
        self,
        *,
        minimize_to_tray: bool,
        autostart: bool,
        proxy_running: bool = False,
        proxy_url: str = "",
    ) -> None:
        """把设置里的开关同步到菜单勾选状态（不触发信号）。"""
        self.tray_action.setChecked(bool(minimize_to_tray))
        self.autostart_action.setChecked(bool(autostart))
        self.set_proxy_running(proxy_running, proxy_url)

    # ------------------------------------------------------------------ events
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # 单击/双击托盘都回到主窗口，这是 Windows 上最符合直觉的行为。
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.showRequested.emit()
        elif reason == QSystemTrayIcon.ActivationReason.Context:
            self.popup_menu()

    def popup_menu(self, anchor: QPoint | None = None) -> None:
        """在托盘图标旁边弹出菜单，并保证整块菜单落在可用屏幕内。"""
        menu = self._menu
        menu.ensurePolished()
        menu.adjustSize()
        size = menu.size()
        anchor = anchor or QCursor.pos()
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if area is None:
            menu.popup(anchor)
            return
        x = anchor.x() - size.width() // 2
        x = max(area.left() + 4, min(x, area.right() - size.width() - 4))
        y = anchor.y() - size.height() - 8
        if y < area.top() + 4:
            y = area.top() + 4
        if y + size.height() > area.bottom() - 4:
            y = max(area.top() + 4, area.bottom() - size.height() - 4)
        menu.popup(QPoint(x, y))

    def notify(self, title: str, message: str, *, msec: int = 4000) -> None:
        if not self.supportsMessages():
            return
        self.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, msec)
