"""Main window: fluent navigation shell around the four pages."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QWidget
from qfluentwidgets import FluentIcon, FluentWindow, NavigationItemPosition, qconfig, setThemeColor

from ..core import settings
from ..core.autostart import AutostartError, is_enabled as autostart_enabled
from ..core.autostart import set_enabled as set_autostart
from ..core.batch import SUPPORTED_SUFFIXES
from ..core.config import APP_NAME, VERSION, data_dir, resource_path
from ..core.global_hotkey import HotkeyError, GlobalHotkey

SHOT_HOTKEY_ID = 0xB17F
from ..core.glossary_db import GlossaryDB
from ..core.history_db import HistoryDB
from ..core.proxy_manager import ProxyManager
from ..core.selection import copy_selection
from .batch_page import BatchPage
from .glossary_page import GlossaryPage
from .history_page import HistoryPage
from .popup import TranslatePopup
from .settings_page import SettingsPage
from .theme import ACCENT, refresh_theme
from .tray import TrayIcon, app_icon
from .translate_page import TranslatePage
from .widgets import IMAGE_SUFFIXES


class MainWindow(FluentWindow):
    def __init__(self) -> None:
        super().__init__()
        setThemeColor(ACCENT)
        self.setWindowTitle(APP_NAME)
        self._nav_expanded = False
        self._quitting = False
        icon_path = resource_path("assets", "icon.ico")
        if Path(icon_path).is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(1180, 760)
        self.setMinimumSize(960, 620)
        self.setAcceptDrops(True)

        self.history_db = HistoryDB(Path(data_dir()) / "history.db")
        self.glossary_db = GlossaryDB(Path(data_dir()) / "glossary.db")
        # 代理跟着窗口活，关掉窗口（收进托盘）后仍然对外服务。
        self.proxy_manager = ProxyManager(self, glossary=self.glossary_db)

        self.translate_page = TranslatePage(self.history_db, self.glossary_db, self)
        self.history_page = HistoryPage(self.history_db, self)
        self.glossary_page = GlossaryPage(self.glossary_db, self)
        self.batch_page = BatchPage(self)
        self.settings_page = SettingsPage(self.proxy_manager, self)

        self.translate_page.setObjectName("translateView")
        self.history_page.setObjectName("historyView")
        self.glossary_page.setObjectName("glossaryView")
        self.batch_page.setObjectName("batchView")
        self.settings_page.setObjectName("settingsView")

        self.addSubInterface(self.translate_page, FluentIcon.LANGUAGE, "翻译")
        self.addSubInterface(self.history_page, FluentIcon.HISTORY, "历史")
        self.addSubInterface(self.glossary_page, FluentIcon.DICTIONARY, "术语表")
        self.addSubInterface(self.batch_page, FluentIcon.TILES, "批量")

        self.navigationInterface.addSeparator()
        # A top-level window has nowhere to go "back" to.
        self.navigationInterface.setReturnButtonVisible(False)
        self.addSubInterface(
            self.settings_page,
            FluentIcon.SETTING,
            "设置",
            position=NavigationItemPosition.BOTTOM,
        )

        self.tray: TrayIcon | None = None
        self._create_tray()

        # 全局划词浮窗：热键由独立线程监听，主窗口没焦点也有效。
        self.popup = TranslatePopup(self.glossary_db, self)
        self.popup.openMainRequested.connect(self._load_text_into_main)
        self.hotkey = GlobalHotkey(self)
        self.hotkey.triggered.connect(self.trigger_selection_popup)
        self.shot_hotkey = GlobalHotkey(self, hotkey_id=SHOT_HOTKEY_ID)
        self.shot_hotkey.triggered.connect(self.trigger_screenshot)

        self._connect_signals()
        self._install_shortcuts()
        self.translate_page.sync_toggles()
        self._apply_always_on_top(bool(settings.get("always_on_top", False)))
        self._restore_geometry()
        self.translate_page.focus_input()

    # ------------------------------------------------------------------ tray
    def _create_tray(self) -> None:
        """建托盘图标；没有系统托盘时静默跳过，功能降级但不崩。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = TrayIcon(self)
        tray.showRequested.connect(self.show_from_tray)
        tray.quitRequested.connect(self.quit_app)
        tray.proxyToggleRequested.connect(self._toggle_proxy_from_tray)
        tray.popupRequested.connect(self.trigger_selection_popup)
        tray.minimizeToTrayChanged.connect(self._set_minimize_to_tray)
        tray.autostartChanged.connect(self._set_autostart)
        tray.set_state(
            minimize_to_tray=bool(settings.get("minimize_to_tray", True)),
            autostart=autostart_enabled(),
        )
        tray.set_popup_hotkey(str(settings.get("popup_hotkey", "")))
        tray.set_shot_hotkey(str(settings.get("shot_hotkey", "")))
        tray.screenshotRequested.connect(self.trigger_screenshot)
        tray.show()
        self.tray = tray

    @property
    def minimize_to_tray(self) -> bool:
        return self.tray is not None and bool(settings.get("minimize_to_tray", True))

    def _set_minimize_to_tray(self, enabled: bool) -> None:
        settings.set("minimize_to_tray", bool(enabled))
        settings.save()
        self.settings_page.reload()

    def _set_autostart(self, enabled: bool) -> None:
        try:
            set_autostart(bool(enabled))
        except AutostartError as exc:
            if self.tray is not None:
                self.tray.notify("开机自启设置失败", str(exc))
            if self.tray is not None:
                self.tray.autostart_action.setChecked(autostart_enabled())
            return
        settings.set("autostart", bool(enabled))
        settings.save()
        self.settings_page.reload()

    def _toggle_proxy_from_tray(self) -> None:
        if self.proxy_manager.running:
            self.proxy_manager.stop()
        else:
            self.proxy_manager.start()

    # ---------------------------------------------------------- 划词浮窗
    def register_hotkey(self, sequence: str | None = None) -> tuple[bool, str]:
        """按设置注册全局热键，返回 (是否成功, 序列或错误信息)。"""
        sequence = str(sequence if sequence is not None else settings.get("popup_hotkey", "Ctrl+Alt+T"))
        sequence = sequence.strip()
        if not bool(settings.get("popup_enabled", True)):
            self.hotkey.unregister()
            return False, "划词浮窗已关闭"
        try:
            self.hotkey.register(sequence)
        except HotkeyError as exc:
            self.hotkey.unregister()
            return False, str(exc)
        return True, sequence

    def trigger_selection_popup(self) -> None:
        """热键回调：备份剪贴板 → 模拟复制 → 弹出浮窗。"""
        text = copy_selection()
        self.popup.show_for(text)
        if not text and self.tray is not None:
            self.tray.notify("没有取到文字", "先用鼠标选中要翻译的文字，再按快捷键。")

    def _load_text_into_main(self, text: str) -> None:
        self.show_from_tray()
        self.switchTo(self.translate_page)
        self.translate_page.load_text(text)

    def trigger_screenshot(self) -> None:
        """全局截屏：先抓屏（此时主窗口还不在画面上），再显示窗口等结果。"""
        if self.translate_page.is_busy():
            self.show_from_tray()
            self.switchTo(self.translate_page)
            return
        self.translate_page.start_screenshot()
        self.show_from_tray()
        self.switchTo(self.translate_page)

    def register_shot_hotkey(self, sequence: str | None = None) -> tuple[bool, str]:
        sequence = str(
            sequence if sequence is not None else settings.get("shot_hotkey", "Ctrl+Alt+S")
        ).strip()
        try:
            self.shot_hotkey.register(sequence)
        except HotkeyError as exc:
            self.shot_hotkey.unregister()
            return False, str(exc)
        return True, sequence

    def _on_hotkey_changed(self, sequence: str) -> None:
        ok, detail = self.register_hotkey(sequence)
        self.settings_page.report_hotkey(ok, detail)

    def _on_shot_hotkey_changed(self, sequence: str) -> None:
        ok, detail = self.register_shot_hotkey(sequence)
        self.settings_page.report_shot_hotkey(ok, detail)

    def _apply_always_on_top(self, on: bool) -> None:
        """切换窗口置顶；Qt 改窗口标志会隐藏窗口，所以可见时要重新 show。"""
        current = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        if current == bool(on):
            return
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(on))
        if visible:
            self.show()
            self.raise_()

    # ---------------------------------------------------------- background work
    def start_background_services(self) -> None:
        """启动时按设置自动拉起本地代理，供沉浸式翻译等外部工具使用。"""
        for signal in (self.proxy_manager.started, self.proxy_manager.stopped):
            signal.connect(self._sync_proxy_ui)
        self.proxy_manager.failed.connect(self._on_proxy_failed)
        if bool(settings.get("autostart_proxy", True)) and not self.proxy_manager.running:
            self.proxy_manager.start()
        ok, detail = self.register_hotkey()
        self.settings_page.report_hotkey(ok, detail)
        ok, detail = self.register_shot_hotkey()
        self.settings_page.report_shot_hotkey(ok, detail)

    def _sync_proxy_ui(self, *_) -> None:
        if self.tray is not None:
            self.tray.set_proxy_running(self.proxy_manager.running, self.proxy_manager.url)
        self.settings_page.sync_proxy_state()

    def _on_proxy_failed(self, message: str) -> None:
        self._sync_proxy_ui()
        if self.tray is not None and not self.isVisible():
            self.tray.notify("本地代理未启动", message)

    def show_from_tray(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._quitting = True
        if self.tray is not None:
            self.tray.hide()
        self.close()
        QApplication.quit()

    # --------------------------------------------------------------- wiring
    def _connect_signals(self) -> None:
        self.translate_page.historyChanged.connect(self.history_page.refresh)
        self.history_page.loadRequested.connect(self._load_history_entry)
        self.settings_page.settingsChanged.connect(self.translate_page.reload_settings)
        self.settings_page.settingsChanged.connect(self._sync_tray_settings)
        self.settings_page.hotkeyChanged.connect(self._on_hotkey_changed)
        self.settings_page.shotHotkeyChanged.connect(self._on_shot_hotkey_changed)
        self.translate_page.pinChanged.connect(self._apply_always_on_top)
        self.settings_page.themeChanged.connect(lambda _: self._reapply_theme())
        for name in ("themeChanged", "themeChangedFinished"):
            signal = getattr(qconfig, name, None)
            if signal is None:
                continue
            try:
                signal.connect(lambda *_: self._reapply_theme())
            except Exception:
                pass

    def _sync_tray_settings(self) -> None:
        if self.tray is None:
            return
        self.tray.set_state(
            minimize_to_tray=bool(settings.get("minimize_to_tray", True)),
            autostart=autostart_enabled(),
            proxy_running=self.proxy_manager.running,
            proxy_url=self.proxy_manager.url,
        )
        self.tray.set_popup_hotkey(str(settings.get("popup_hotkey", "")))
        self.tray.set_shot_hotkey(str(settings.get("shot_hotkey", "")))

    def _reapply_theme(self) -> None:
        refresh_theme()
        self.translate_page.apply_theme()

    # ------------------------------------------------------------ navigation
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._nav_expanded:
            return
        self._nav_expanded = True
        # FluentWindow starts with a compact rail; desktop widths have room for labels.
        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.expand(useAni=False)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Below ~1010 px the panel auto-collapses; re-expand once there is room again.
        panel = self.navigationInterface.panel
        if panel.isCollapsed() and self.width() >= 1010 and not panel.isMinimalEnabled:
            self.navigationInterface.expand(useAni=False)

    def _install_shortcuts(self) -> None:
        bindings = [
            ("Ctrl+Return", self.translate_page.start_translation),
            ("Ctrl+Enter", self.translate_page.start_translation),
            ("Ctrl+Shift+C", self.translate_page.copy_result),
            ("Ctrl+L", self.translate_page.clear_all),
            ("Ctrl+1", lambda: self.switchTo(self.translate_page)),
            ("Ctrl+2", lambda: self.switchTo(self.history_page)),
            ("Ctrl+3", lambda: self.switchTo(self.glossary_page)),
            ("Ctrl+4", lambda: self.switchTo(self.batch_page)),
            ("Ctrl+5", lambda: self.switchTo(self.settings_page)),
        ]
        for sequence, handler in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)

    def _load_history_entry(self, entry) -> None:
        self.switchTo(self.translate_page)
        self.translate_page.load_entry(entry)

    # ------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._dropped_files(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        files = self._dropped_files(event)
        if not files:
            return
        event.acceptProposedAction()
        images = [path for path in files if Path(path).suffix.lower() in IMAGE_SUFFIXES]
        # 单张图片走翻译页；多文件 / 文件夹 / 文档走批量队列。
        if len(files) == 1 and images and Path(files[0]).is_file():
            self.switchTo(self.translate_page)
            self.translate_page.set_image_from_path(files[0])
            return
        self.switchTo(self.batch_page)
        self.batch_page.add_paths(files)

    @staticmethod
    def _dropped_files(event) -> list[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        files: list[str] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            candidate = Path(url.toLocalFile())
            if candidate.is_dir() or candidate.suffix.lower() in SUPPORTED_SUFFIXES:
                files.append(str(candidate))
        return files

    # ------------------------------------------------------------- geometry
    def _restore_geometry(self) -> None:
        geometry = settings.get("window_geometry", "")
        if geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(str(geometry).encode("ascii")))
                return
            except Exception:
                pass
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(
                area.x() + (area.width() - self.width()) // 2,
                area.y() + (area.height() - self.height()) // 2,
            )

    # --------------------------------------------------------------- closing
    def _persist_geometry(self) -> None:
        try:
            settings.set(
                "window_geometry", bytes(self.saveGeometry().toBase64()).decode("ascii")
            )
            settings.save()
        except Exception:
            pass

    def _shutdown(self) -> None:
        self.translate_page.flush_draft()
        self.batch_page.shutdown()
        self.settings_page.shutdown()
        self.hotkey.unregister()
        self.shot_hotkey.unregister()
        self.popup.dismiss()
        self.proxy_manager.shutdown()
        self.history_db.close()
        self.glossary_db.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._persist_geometry()

        if not self._quitting and self.minimize_to_tray:
            # 关窗口 = 进托盘：本地代理、全局热键、剪贴板监听都继续工作。
            event.ignore()
            self.hide()
            self._maybe_show_tray_tip()
            return

        try:
            self._shutdown()
        except Exception:
            pass
        if self.tray is not None:
            self.tray.hide()
        super().closeEvent(event)

    def _maybe_show_tray_tip(self) -> None:
        """第一次收进托盘时告诉用户怎么找回来，之后不再打扰。"""
        if self.tray is None or bool(settings.get("tray_tip_shown", False)):
            return
        settings.set("tray_tip_shown", True)
        settings.save()
        self.tray.notify(
            "IndexTranslate 仍在后台运行",
            "本地代理与快捷键保持可用。单击托盘图标可以回到主窗口，右键菜单里可以退出。",
        )
