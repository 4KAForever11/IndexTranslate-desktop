"""Main window: fluent navigation shell around the four pages."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QWidget
from qfluentwidgets import FluentIcon, FluentWindow, NavigationItemPosition, qconfig, setThemeColor

from ..core import settings
from ..core.config import APP_NAME, VERSION, data_dir, resource_path
from ..core.glossary_db import GlossaryDB
from ..core.history_db import HistoryDB
from .glossary_page import GlossaryPage
from .history_page import HistoryPage
from .settings_page import SettingsPage
from .theme import ACCENT, refresh_theme
from .translate_page import TranslatePage
from .widgets import IMAGE_SUFFIXES


class MainWindow(FluentWindow):
    def __init__(self) -> None:
        super().__init__()
        setThemeColor(ACCENT)
        self.setWindowTitle(APP_NAME)
        self._nav_expanded = False
        icon_path = resource_path("assets", "icon.ico")
        if Path(icon_path).is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(1180, 760)
        self.setMinimumSize(960, 620)
        self.setAcceptDrops(True)

        self.history_db = HistoryDB(Path(data_dir()) / "history.db")
        self.glossary_db = GlossaryDB(Path(data_dir()) / "glossary.db")

        self.translate_page = TranslatePage(self.history_db, self.glossary_db, self)
        self.history_page = HistoryPage(self.history_db, self)
        self.glossary_page = GlossaryPage(self.glossary_db, self)
        self.settings_page = SettingsPage(self)

        self.translate_page.setObjectName("translateView")
        self.history_page.setObjectName("historyView")
        self.glossary_page.setObjectName("glossaryView")
        self.settings_page.setObjectName("settingsView")

        self.addSubInterface(self.translate_page, FluentIcon.LANGUAGE, "翻译")
        self.addSubInterface(self.history_page, FluentIcon.HISTORY, "历史")
        self.addSubInterface(self.glossary_page, FluentIcon.DICTIONARY, "术语表")

        self.navigationInterface.addSeparator()
        # A top-level window has nowhere to go "back" to.
        self.navigationInterface.setReturnButtonVisible(False)
        self.addSubInterface(
            self.settings_page,
            FluentIcon.SETTING,
            "设置",
            position=NavigationItemPosition.BOTTOM,
        )

        self._connect_signals()
        self._install_shortcuts()
        self._restore_geometry()
        self.translate_page.focus_input()

    # --------------------------------------------------------------- wiring
    def _connect_signals(self) -> None:
        self.translate_page.historyChanged.connect(self.history_page.refresh)
        self.history_page.loadRequested.connect(self._load_history_entry)
        self.settings_page.settingsChanged.connect(self.translate_page.reload_settings)
        self.settings_page.themeChanged.connect(lambda _: self._reapply_theme())
        for name in ("themeChanged", "themeChangedFinished"):
            signal = getattr(qconfig, name, None)
            if signal is None:
                continue
            try:
                signal.connect(lambda *_: self._reapply_theme())
            except Exception:
                pass

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
            ("Ctrl+4", lambda: self.switchTo(self.settings_page)),
        ]
        for sequence, handler in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)

    def _load_history_entry(self, entry) -> None:
        self.switchTo(self.translate_page)
        self.translate_page.load_entry(entry)

    # ------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._dropped_image(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = self._dropped_image(event)
        if path:
            event.acceptProposedAction()
            self.switchTo(self.translate_page)
            self.translate_page.set_image_from_path(path)

    @staticmethod
    def _dropped_image(event) -> str | None:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                candidate = url.toLocalFile()
                if Path(candidate).suffix.lower() in IMAGE_SUFFIXES:
                    return candidate
        return None

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

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            settings.set(
                "window_geometry", bytes(self.saveGeometry().toBase64()).decode("ascii")
            )
            settings.save()
        except Exception:
            pass
        try:
            self.settings_page.shutdown()
        except Exception:
            pass
        self.history_db.close()
        self.glossary_db.close()
        super().closeEvent(event)
