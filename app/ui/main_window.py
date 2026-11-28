"""Main window: fluent navigation shell around the four pages."""

from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import QByteArray, QSysInfo, Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QWidget
from qfluentwidgets import FluentIcon, FluentWindow, NavigationItemPosition, qconfig, setThemeColor

from ..core import i18n, settings
from ..core.autostart import AutostartError, is_enabled as autostart_enabled
from ..core.autostart import set_enabled as set_autostart
from ..core.i18n import t as tr
from ..core.parsers import is_supported
from ..core.config import (
    APP_NAME,
    DEFAULT_MODEL,
    MODEL_IDS,
    PROJECT_URL,
    VERSION,
    data_dir,
    model_label,
    resource_path,
)
from ..core.extensions.runtime import ExtensionRuntime
from ..core.glossary_db import GlossaryDB
from ..core.history_db import HistoryDB
from ..core.pipeline import configure_pipeline, get_pipeline
from ..core.proxy_manager import ProxyManager
from ..core.sync.webdav_sync import SyncService
from ..system.clipboard_bridge import capture_for_replace, capture_selected_text, is_blocked
from ..system.clipboard_bridge import replace_selection_with_text
from ..system.hotkeys import HotkeyError, HotkeyManager
from ..system.ocr_bridge import OCR_ID, OcrTask
from ..system.single_instance import SingleInstance
from ..workers.translation_worker import TranslationRequest, TranslationWorker
from .batch_page import BatchPage
from .capture_overlay import CaptureOverlay
from .extensions_page import ExtensionsPage
from .glossary_page import GlossaryPage
from .history_page import HistoryPage
from .live_subtitle_bar import ASR_ID, LiveSubtitleController, SubtitleBar
from .overlay_renderer import TranslationOverlay, segment_patches
from .popup_window import TranslatePopup
from .settings_page import SettingsPage
from .theme import ACCENT, refresh_theme
from .tray import TrayIcon, app_icon
from .translate_page import TranslatePage
from .widgets import IMAGE_SUFFIXES

MAX_OCR_SEGMENTS = 60


def _is_windows_11() -> bool:
    """Win11 的构建号从 22000 起；Win10 上不要开 Mica，否则窗口会变透明。"""
    try:
        build = QSysInfo.kernelVersion().rsplit(".", 1)[-1]
        return int(build) >= 22000
    except (ValueError, AttributeError):
        return False


class MainWindow(FluentWindow):
    def __init__(self) -> None:
        super().__init__()
        setThemeColor(ACCENT)
        self.setWindowTitle(APP_NAME)
        self._nav_expanded = False
        self._quitting = False
        self._shut_down = False
        icon_path = resource_path("assets", "icon.ico")
        if Path(icon_path).is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(1180, 760)
        self.setMinimumSize(960, 620)
        self.setAcceptDrops(True)

        self.history_db = HistoryDB(Path(data_dir()) / "history.db")
        self.glossary_db = GlossaryDB(Path(data_dir()) / "glossary.db")
        # 底座：术语注入 / 缓存 / 限流 / 降级全部走同一个管道。
        configure_pipeline(glossary_db=self.glossary_db)
        # 代理跟着窗口活，关掉窗口（收进托盘）后仍然对外服务。
        self.proxy_manager = ProxyManager(self, glossary=self.glossary_db)
        # 重型能力（PDF / OCR / ASR）都在 Sidecar 子进程里，这里只管它们的生命周期。
        self.extensions = ExtensionRuntime(self)
        self._mica_applied = False
        # 跨设备同步：只碰术语 / 风格 / 偏好，缓存与批量任务的绝对路径绝不上云。
        self.sync_service = SyncService(
            self.glossary_db,
            meta_path=Path(data_dir()) / "sync_meta.db",
            db_path=Path(data_dir()) / "glossary.db",
            settings_store=settings,
        )

        self.translate_page = TranslatePage(self.history_db, self.glossary_db, self)
        self.history_page = HistoryPage(self.history_db, self)
        self.glossary_page = GlossaryPage(self.glossary_db, self)
        self.batch_page = BatchPage(self)
        self.extensions_page = ExtensionsPage(self.extensions, self)
        self.settings_page = SettingsPage(self.proxy_manager, self.sync_service, self)

        self.translate_page.setObjectName("translateView")
        self.history_page.setObjectName("historyView")
        self.glossary_page.setObjectName("glossaryView")
        self.batch_page.setObjectName("batchView")
        self.extensions_page.setObjectName("extensionsView")
        self.settings_page.setObjectName("settingsView")

        self.addSubInterface(self.translate_page, FluentIcon.LANGUAGE, tr("nav.translate"))
        self.addSubInterface(self.history_page, FluentIcon.HISTORY, tr("nav.history"))
        self.addSubInterface(self.glossary_page, FluentIcon.DICTIONARY, tr("nav.glossary"))
        self.addSubInterface(self.batch_page, FluentIcon.TILES, tr("nav.batch"))
        self.addSubInterface(self.extensions_page, FluentIcon.APPLICATION, tr("nav.extensions"))

        self.navigationInterface.addSeparator()
        # A top-level window has nowhere to go "back" to.
        self.navigationInterface.setReturnButtonVisible(False)
        self.addSubInterface(
            self.settings_page,
            FluentIcon.SETTING,
            tr("nav.settings"),
            position=NavigationItemPosition.BOTTOM,
        )

        self.tray: TrayIcon | None = None
        self._overlay: CaptureOverlay | None = None
        self._patch_overlay: TranslationOverlay | None = None
        self._ocr_task: OcrTask | None = None
        self._replacing = False
        self._create_tray()

        # 全局热键统一交给 HotkeyManager：RegisterHotKey + Qt 原生事件过滤器，
        # 回调直接落在主线程，不必起线程，也不引入国产杀软讨厌的键盘钩子。
        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self._on_hotkey)

        self.popup = TranslatePopup(self.glossary_db, self)
        self.popup.openMainRequested.connect(self._load_text_into_main)

        # 实时字幕条 + 控制器（扩展没装时 start() 会给出提示，不会崩）
        self.subtitle_bar = SubtitleBar()
        self.subtitle = LiveSubtitleController(self.extensions, self)
        self.subtitle.sentenceReady.connect(self.subtitle_bar.set_text)
        self.subtitle.statusChanged.connect(self.subtitle_bar.set_status)
        self.subtitle.failed.connect(self._on_subtitle_failed)
        self.subtitle_bar.closed.connect(self.subtitle.stop)

        # 空闲扩展进程每 60 秒回收一次，把内存还给系统
        self._reclaim_timer = QTimer(self)
        self._reclaim_timer.setInterval(60_000)
        self._reclaim_timer.timeout.connect(self.extensions.reclaim_idle)
        self._reclaim_timer.start()

        self._connect_signals()
        self._install_shortcuts()
        self.translate_page.sync_toggles()
        self._apply_always_on_top(bool(settings.get("always_on_top", False)))
        self._apply_mica(bool(settings.get("mica", True)))
        self._restore_geometry()
        self.translate_page.focus_input()

    # ------------------------------------------------------------------ 外观
    def _apply_mica(self, enabled: bool) -> None:
        """Win11 上开 Mica 云母背景；Win10 上直接跳过（没有该合成效果）。"""
        if not hasattr(self, "setMicaEffectEnabled"):
            return
        if not _is_windows_11():
            self.setMicaEffectEnabled(False)
            return
        try:
            self.setMicaEffectEnabled(bool(enabled))
            self._mica_applied = bool(enabled)
        except Exception:
            self._mica_applied = False

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
        tray.set_replace_hotkey(str(settings.get("replace_hotkey", "")))
        tray.screenshotRequested.connect(self.trigger_screenshot)
        tray.replaceRequested.connect(self.trigger_replace)
        tray.subtitleRequested.connect(self.toggle_live_subtitle)
        tray.extensionsRequested.connect(self.show_extensions)
        tray.githubRequested.connect(self.open_official_page)
        self._sync_subtitle_action()
        tray.show()
        self.tray = tray

    def show_extensions(self) -> None:
        self.show_from_tray()
        self.switchTo(self.extensions_page)

    def open_official_page(self) -> None:
        """打开官方仓库：用户拿它核对下载来源，防改包伪冒。"""
        if not QDesktopServices.openUrl(QUrl(PROJECT_URL)):
            self.tray.notify("无法打开浏览器", PROJECT_URL)

    # ------------------------------------------------------------- 实时字幕
    def _sync_subtitle_action(self) -> None:
        if self.tray is None:
            return
        running = self.subtitle.running if getattr(self, "subtitle", None) else False
        self.tray.subtitle_action.blockSignals(True)
        self.tray.subtitle_action.setChecked(running)
        self.tray.subtitle_action.blockSignals(False)

    def toggle_live_subtitle(self) -> None:
        """打开 / 关闭实时字幕条。"""
        if self.subtitle.running:
            self.subtitle.stop()
            self.subtitle_bar.hide()
            self._sync_subtitle_action()
            return
        if not self.subtitle.start():
            self._sync_subtitle_action()
            return
        geometry = str(settings.get("live_subtitle_geometry", "") or "")
        if geometry:
            self.subtitle_bar.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        else:
            self.subtitle_bar.move_to_default()
        self.subtitle_bar.show()
        self.subtitle_bar.raise_()
        self._sync_subtitle_action()

    def _on_subtitle_failed(self, message: str) -> None:
        if self.tray is not None:
            self.tray.notify("实时字幕不可用", message[:200])
        self.subtitle.stop()
        self.subtitle_bar.hide()
        self._sync_subtitle_action()

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

    # ------------------------------------------------------ 全局热键 / 浮窗
    def register_hotkey(self, sequence: str | None = None) -> tuple[bool, str]:
        """按设置注册划词浮窗热键，返回 (是否成功, 序列或错误信息)。"""
        if not bool(settings.get("popup_enabled", True)):
            self.hotkeys.unregister("popup")
            return False, "划词浮窗已关闭"
        sequence = str(
            sequence if sequence is not None else settings.get("popup_hotkey", "Ctrl+Alt+T")
        )
        return self._register_action("popup", sequence.strip())

    def register_shot_hotkey(self, sequence: str | None = None) -> tuple[bool, str]:
        sequence = str(
            sequence if sequence is not None else settings.get("shot_hotkey", "Ctrl+Alt+S")
        )
        return self._register_action("capture", sequence.strip())

    def register_replace_hotkey(self, sequence: str | None = None) -> tuple[bool, str]:
        if not bool(settings.get("replace_enabled", True)):
            self.hotkeys.unregister("replace")
            return False, "原地替换已关闭"
        sequence = str(
            sequence if sequence is not None else settings.get("replace_hotkey", "Ctrl+Alt+R")
        )
        return self._register_action("replace", sequence.strip())

    def _register_action(self, action: str, sequence: str) -> tuple[bool, str]:
        if not sequence:
            self.hotkeys.unregister(action)
            return False, "未设置快捷键"
        try:
            self.hotkeys.register(action, sequence)
        except HotkeyError as exc:
            return False, str(exc)
        return True, sequence

    def _on_hotkey(self, action: str) -> None:
        """热键回调（主线程）：分派到三个入口。"""
        if action == "popup":
            self.trigger_selection_popup()
        elif action == "capture":
            self.trigger_screenshot()
        elif action == "replace":
            self.trigger_replace()

    def trigger_selection_popup(self) -> None:
        """取选区 → 弹浮窗。剪贴板原有内容会在取词后还原。"""
        if is_blocked(settings.get("selection_blacklist", [])):
            if self.tray is not None:
                self.tray.notify("已跳过取词", "当前程序在取词黑名单里。")
            return
        text = capture_selected_text()
        self.popup.show_for(text)
        if not text.strip() and self.tray is not None:
            self.tray.notify("没有取到文字", "先用鼠标选中要翻译的文字，再按快捷键。")

    def _load_text_into_main(self, text: str) -> None:
        self.show_from_tray()
        self.switchTo(self.translate_page)
        self.translate_page.load_text(text)

    def trigger_screenshot(self) -> None:
        """框选截屏：先抓屏（此时本窗口不在画面上），再按设置决定结果去向。"""
        target = str(settings.get("capture_target", "main") or "main")
        if target == "overlay" and self._ocr_ready():
            if self._overlay is not None or self._patch_overlay is not None:
                return
            overlay = CaptureOverlay()
            overlay.captured.connect(self._capture_to_overlay)
            overlay.cancelled.connect(self._clear_overlay)
            self._overlay = overlay
            overlay.start()
            return
        if target != "popup":
            if self.translate_page.is_busy():
                self.show_from_tray()
                self.switchTo(self.translate_page)
                return
            self.translate_page.start_screenshot()
            self.show_from_tray()
            self.switchTo(self.translate_page)
            return
        if self._overlay is not None:
            return
        overlay = CaptureOverlay()
        overlay.captured.connect(self._capture_to_popup)
        overlay.cancelled.connect(self._clear_overlay)
        self._overlay = overlay
        overlay.start()

    # ------------------------------------------------------- OCR 原位贴片
    def _ocr_ready(self) -> bool:
        return bool(settings.get("ocr_overlay_enabled", True)) and self.extensions.is_enabled(OCR_ID)

    def _capture_to_overlay(self, image) -> None:
        """截图 → Sidecar OCR → 逐段翻译 → 原位贴片。"""
        self._overlay = None
        if image is None or image.isNull():
            return
        client = self.extensions.client(OCR_ID)
        if client is None:
            self._degrade_to_popup(image, "本地 OCR 扩展不可用，已改用图片翻译。")
            return
        if self._ocr_task is not None and self._ocr_task.isRunning():
            return
        task = OcrTask(client, image, self)
        task.done.connect(self._on_ocr_done)
        task.failed.connect(self._on_ocr_failed)
        task.finished.connect(lambda: setattr(self, "_ocr_task", None))
        self._ocr_task = task
        task.start()

    def _on_ocr_done(self, image, segments) -> None:
        segments = [s for s in segments if str(s.get("text") or "").strip()]
        if not segments:
            self._degrade_to_popup(image, "没有在选区里识别到文字，已改用图片翻译。")
            return
        segments = segments[:MAX_OCR_SEGMENTS]
        model = str(settings.get("replace_model", DEFAULT_MODEL) or DEFAULT_MODEL)
        translations: list[str] = []
        for index, segment in enumerate(segments, start=1):
            if self.tray is not None and index == 1:
                self.tray.notify("正在翻译选区", f"共 {len(segments)} 段，使用 {model_label(model)}")
            translations.append(self._translate_blocking(str(segment.get("text") or ""), model))
        patches = segment_patches(segments, translations)
        if not any(patch.translated for patch in patches):
            self._degrade_to_popup(image, "本地 OCR 成功但翻译失败，已改用图片翻译。")
            return
        overlay = TranslationOverlay()
        overlay.closed.connect(self._clear_patch_overlay)
        self._patch_overlay = overlay
        overlay.show_patches(
            image,
            patches,
            font_scale=float(settings.get("ocr_overlay_font_scale", 1.0) or 1.0),
        )

    def _on_ocr_failed(self, message: str) -> None:
        if self.tray is not None:
            self.tray.notify("本地 OCR 不可用", f"{message[:160]}")
        # 不再重复弹窗：失败就是失败，用户可在扩展中心看测试结果。

    def _clear_patch_overlay(self) -> None:
        self._patch_overlay = None

    def _degrade_to_popup(self, image, reason: str) -> None:
        """OCR 链路出问题时退回「图片翻译」，功能不中断。"""
        if self.tray is not None:
            self.tray.notify("已降级为图片翻译", reason)
        self.popup.show_image(image)

    def _capture_to_popup(self, image) -> None:
        self._overlay = None
        self.popup.show_image(image)

    def _clear_overlay(self) -> None:
        self._overlay = None

    def trigger_replace(self) -> None:
        """原地替换：取选区（必要时 Ctrl+A）→ 翻译 → 粘回原位置。

        这条路径不切窗口、不覆盖用户剪贴板（取词与注入前后都还原）。
        """
        if not bool(settings.get("replace_enabled", True)) or self._replacing:
            return
        if is_blocked(settings.get("selection_blacklist", [])):
            if self.tray is not None:
                self.tray.notify("已跳过替换", "当前程序在取词黑名单里。")
            return
        self._replacing = True
        try:
            if bool(settings.get("replace_select_all", True)):
                text = capture_for_replace()
            else:
                text = capture_selected_text()
            if not text.strip():
                if self.tray is not None:
                    self.tray.notify("没有取到文字", "先选中要替换的文字，或把光标放进输入框。")
                return
            model = str(settings.get("replace_model", DEFAULT_MODEL) or DEFAULT_MODEL)
            result = self._translate_blocking(text, model)
            if not result.strip():
                if self.tray is not None:
                    self.tray.notify("原地替换失败", "没有拿到译文，请检查网络或接口地址。")
                return
            replace_selection_with_text(result)
            if self.tray is not None:
                self.tray.notify("已原地替换", f"{len(result)} 字 · {model_label(model)}")
        finally:
            self._replacing = False

    def _translate_blocking(self, text: str, model: str, timeout: float = 30.0) -> str:
        """同步翻译一次，用于「必须拿到结果才能粘回去」的替换场景。

        注意：worker 的信号是跨线程投递的（queued connection），线程结束后还要把
        主线程的事件队列排空，否则 done/stopped 永远送不到，这里就会傻等到底再返回空串。
        """
        request = TranslationRequest(
            text=text,
            target_lang=str(settings.get("target_lang", "zh") or "zh"),
            source_lang=str(settings.get("source_lang", "auto") or "auto"),
            instruction=str(settings.get("instruction", "") or ""),
            model=model,
            api_base=str(settings.get("api_base", "") or ""),
            timeout=float(settings.get("timeout", 120) or 120),
            stream=True,
            temperature=float(settings.get("temperature", 0.0) or 0.0),
            scene="polish",
        )
        state = {"text": "", "settled": False}

        def _settle(text_value: str = "") -> None:
            state["text"] = text_value
            state["settled"] = True

        worker = TranslationWorker(request)
        worker.done.connect(lambda result, _ms: _settle(result))
        worker.stopped.connect(lambda partial: _settle(partial))
        worker.failed.connect(lambda _message, _detail: _settle(""))

        deadline = time.monotonic() + max(5.0, timeout)
        worker.start()
        while not worker.isFinished() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        if worker.isFinished():
            worker.wait(2000)
        else:
            worker.cancel()
            worker.wait(1500)

        # 线程已经结束，但结果可能还排在事件队列里，给一小段时间把它取出来。
        grace = time.monotonic() + 2.0
        while not state["settled"] and time.monotonic() < grace:
            QApplication.processEvents()
            time.sleep(0.01)
        return state["text"]

    def _on_hotkey_changed(self, sequence: str) -> None:
        ok, detail = self.register_hotkey(sequence)
        self.settings_page.report_hotkey(ok, detail)

    def _on_shot_hotkey_changed(self, sequence: str) -> None:
        ok, detail = self.register_shot_hotkey(sequence)
        self.settings_page.report_shot_hotkey(ok, detail)

    def _on_replace_hotkey_changed(self, sequence: str) -> None:
        ok, detail = self.register_replace_hotkey(sequence)
        self.settings_page.report_replace_hotkey(ok, detail)

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
    def _on_language_changed(self, code: str) -> None:
        """文案在界面构建时就写进了控件，切换语言统一在下次启动生效。"""
        label = dict(i18n.LANGUAGES).get(code, code)
        text = f"界面语言已切换为「{label}」，重新打开程序后全部生效。"
        if self.tray is not None:
            self.tray.notify("界面语言", text)

    def _auto_sync(self) -> None:
        service = getattr(self, "sync_service", None)
        if service is None:
            return
        try:
            result = service.sync_now()
        except Exception as exc:  # noqa: BLE001 - 自动同步失败只提示，不打断使用
            settings.set("sync_last_result", f"自动同步失败：{exc}")
            settings.save()
            return
        if self.tray is not None and result.ok:
            self.tray.notify("同步完成", result.message or "与云端一致")

    def start_background_services(self) -> None:
        """启动时按设置自动拉起本地代理，并装好全局热键。"""
        for signal in (self.proxy_manager.started, self.proxy_manager.stopped):
            signal.connect(self._sync_proxy_ui)
        self.proxy_manager.failed.connect(self._on_proxy_failed)
        if bool(settings.get("autostart_proxy", True)) and not self.proxy_manager.running:
            self.proxy_manager.start()

        # 自动同步放在后台线程里跑：WebDAV 慢的时候不能卡住启动。
        if bool(settings.get("sync_auto", False)) and str(settings.get("sync_url", "") or ""):
            QTimer.singleShot(1500, self._auto_sync)

        # 热键装不上（被占用 / 权限）不能让程序挂掉，只在设置页给出一行提示。
        try:
            self.hotkeys.install()
        except HotkeyError as exc:
            for reporter in (
                self.settings_page.report_hotkey,
                self.settings_page.report_shot_hotkey,
                self.settings_page.report_replace_hotkey,
            ):
                reporter(False, str(exc))
            return
        ok, detail = self.register_hotkey()
        self.settings_page.report_hotkey(ok, detail)
        ok, detail = self.register_shot_hotkey()
        self.settings_page.report_shot_hotkey(ok, detail)
        ok, detail = self.register_replace_hotkey()
        self.settings_page.report_replace_hotkey(ok, detail)

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
        self.settings_page.settingsChanged.connect(self._on_settings_changed)
        self.settings_page.settingsChanged.connect(self._sync_tray_settings)
        self.settings_page.hotkeyChanged.connect(self._on_hotkey_changed)
        self.settings_page.shotHotkeyChanged.connect(self._on_shot_hotkey_changed)
        self.settings_page.replaceHotkeyChanged.connect(self._on_replace_hotkey_changed)
        self.translate_page.pinChanged.connect(self._apply_always_on_top)
        self.settings_page.micaChanged.connect(self._apply_mica)
        self.settings_page.languageChanged.connect(self._on_language_changed)
        self.settings_page.themeChanged.connect(lambda _: self._reapply_theme())
        for name in ("themeChanged", "themeChangedFinished"):
            signal = getattr(qconfig, name, None)
            if signal is None:
                continue
            try:
                signal.connect(lambda *_: self._reapply_theme())
            except Exception:
                pass

    def _on_settings_changed(self) -> None:
        """Any settings change may move the pipeline's limits/cache/fallback."""
        self.translate_page.reload_settings()
        get_pipeline().reload_settings()

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
        self.tray.set_replace_hotkey(str(settings.get("replace_hotkey", "")))

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
            ("Ctrl+5", lambda: self.switchTo(self.extensions_page)),
            ("Ctrl+6", lambda: self.switchTo(self.settings_page)),
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
            if candidate.is_dir() or is_supported(candidate):
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
        # 退出路径有好几条（关窗、托盘退出、--selftest、异常清理），必须可重入，
        # 否则第二次 close() 会去关已经关掉的数据库 / 已经停掉的代理。
        if self._shut_down:
            return
        self._shut_down = True
        self.translate_page.flush_draft()
        self._reclaim_timer.stop()
        try:
            self._persist_subtitle_geometry()
            self.subtitle.stop()
            self.subtitle_bar.hide()
            if self._ocr_task is not None and self._ocr_task.isRunning():
                self._ocr_task.wait(2000)
            self.extensions.shutdown()
        except Exception:
            pass
        self.batch_page.shutdown()
        self.settings_page.shutdown()
        self.hotkeys.unregister_all()
        self.popup.dismiss()
        if self._patch_overlay is not None:
            self._patch_overlay.close_overlay()
            self._patch_overlay = None
        self.proxy_manager.shutdown()
        self.history_db.close()
        self.glossary_db.close()

    def _persist_subtitle_geometry(self) -> None:
        if self.subtitle_bar.isVisible():
            geometry = bytes(self.subtitle_bar.saveGeometry().toBase64()).decode("ascii")
            settings.set("live_subtitle_geometry", geometry)
            settings.set("live_subtitle_enabled", self.subtitle.running)
            settings.save()

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
