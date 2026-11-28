"""Settings: service, translation behaviour, appearance, local proxy, about."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox,
    DoubleSpinBox,
    FluentIcon,
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SmoothScrollArea,
    SpinBox,
    SwitchButton,
    Theme,
    TitleLabel,
    setTheme,
)

from ..core import i18n, settings
from ..core.autostart import AutostartError, is_enabled as autostart_enabled
from ..core.autostart import set_enabled as set_autostart
from ..core.config import (
    API_BASE,
    APP_PROJECT,
    MODELS,
    PROJECT_RELEASES_URL,
    PROJECT_URL,
    SERVICE_HOME,
    VERSION,
    data_dir,
)
from ..core.proxy_manager import ProxyManager
from ..core.sync.webdav_sync import SyncService
from .theme import Card, MutedLabel, PageWidget, TextLabel

THEMES = [("auto", "跟随系统"), ("light", "浅色"), ("dark", "深色")]


def _title_label(text: str) -> QLabel:
    return TextLabel(text, size=13, bold=True)


class SettingsPage(PageWidget):
    themeChanged = pyqtSignal(str)
    settingsChanged = pyqtSignal()
    hotkeyChanged = pyqtSignal(str)
    shotHotkeyChanged = pyqtSignal(str)
    replaceHotkeyChanged = pyqtSignal(str)
    micaChanged = pyqtSignal(bool)
    languageChanged = pyqtSignal(str)

    def __init__(
        self,
        proxy_manager: ProxyManager,
        sync_service: SyncService | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # 代理的所有权在 MainWindow，设置页只驱动它并展示状态。
        self._proxy_manager = proxy_manager
        self._sync_service = sync_service
        self._updater = None
        self._update_worker = None
        self._download_worker = None
        self._last_status = None
        self._auto_checked = False
        self._busy_sync = False
        self._build_ui()
        self._sync_from_settings()
        self._proxy_manager.started.connect(self._on_proxy_started)
        self._proxy_manager.stopped.connect(self._on_proxy_stopped)
        self._proxy_manager.failed.connect(self._on_proxy_failed)
        # 启动 6 秒后再查（先把首屏和翻译服务热起来），只查一次。
        QTimer.singleShot(6000, self._auto_check_once)

    def _on_update_auto_changed(self, checked: bool) -> None:
        self._save("update_check_on_start", bool(checked))

    def _auto_check_once(self) -> None:
        if self._auto_checked:
            return
        self._auto_checked = True
        self._auto_check_update()

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 12)
        outer.setSpacing(10)
        outer.addWidget(TitleLabel("设置"))

        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(SmoothScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("SmoothScrollArea { background: transparent; }")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 4, 6, 8)
        layout.setSpacing(14)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        layout.addWidget(self._build_service_card())
        layout.addWidget(self._build_behaviour_card())
        layout.addWidget(self._build_core_card())
        layout.addWidget(self._build_appearance_card())
        layout.addWidget(self._build_sync_card())
        layout.addWidget(self._build_update_card())
        layout.addWidget(self._build_system_card())
        layout.addWidget(self._build_proxy_card())
        layout.addWidget(self._build_proxy_optimize_card())
        layout.addWidget(self._build_about_card())
        layout.addStretch(1)

    def _card(self, title: str) -> tuple[Card, QVBoxLayout]:
        card = Card(radius=10)
        body = QVBoxLayout(card)
        body.setContentsMargins(20, 16, 20, 18)
        body.setSpacing(14)
        body.addWidget(_title_label(title))
        return card, body

    @staticmethod
    def _row(layout: QVBoxLayout, title: str, subtitle: str, control: QWidget) -> None:
        row = QHBoxLayout()
        row.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(_title_label(title))
        if subtitle:
            text.addWidget(MutedLabel(subtitle, size=11))
        row.addLayout(text, 1)
        control.setMinimumWidth(220)
        row.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(row)

    def _build_service_card(self) -> Card:
        card, body = self._card("翻译服务")
        body.addWidget(
            MutedLabel("免费公网接口，无需 API Key；如接口调整，可在这里修改地址。", size=11)
        )

        api_row = QHBoxLayout()
        api_row.setSpacing(8)
        self.api_edit = LineEdit()
        self.api_edit.setPlaceholderText(API_BASE)
        self.api_edit.setClearButtonEnabled(True)
        self.api_edit.setFixedHeight(34)
        self.api_reset_button = PushButton("恢复默认")
        self.api_reset_button.setMinimumWidth(96)
        self.api_reset_button.setFixedHeight(34)
        api_row.addWidget(self.api_edit, 1)
        api_row.addWidget(self.api_reset_button)
        body.addWidget(_title_label("接口地址"))
        body.addLayout(api_row)

        self.model_combo = ComboBox()
        self.model_combo.setMinimumWidth(260)
        self._row(body, "模型", "35B 质量最好，2B 响应最快", self.model_combo)
        for model_id, label in MODELS:
            self.model_combo.addItem(label, userData=model_id)

        self.timeout_spin = SpinBox()
        self.timeout_spin.setRange(10, 300)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setMinimumWidth(160)
        self._row(body, "请求超时", "长文本或服务拥塞时可调大", self.timeout_spin)

        self.api_edit.editingFinished.connect(self._save_api_base)
        self.api_reset_button.clicked.connect(self._reset_api_base)
        self.model_combo.currentIndexChanged.connect(self._save_model)
        self.timeout_spin.valueChanged.connect(
            lambda value: self._save("timeout", int(value))
        )
        return card

    def _build_behaviour_card(self) -> Card:
        card, body = self._card("翻译选项")

        self.stream_switch = SwitchButton()
        self._row(body, "流式输出", "边生成边显示，长文本体感更快", self.stream_switch)

        self.glossary_switch = SwitchButton()
        self._row(body, "启用术语表", "把术语表作为硬性要求附加到每次翻译", self.glossary_switch)

        self.keep_spin = SpinBox()
        self.keep_spin.setRange(0, 5000)
        self.keep_spin.setSuffix(" 条")
        self.keep_spin.setMinimumWidth(160)
        self._row(body, "历史保留条数", "0 表示不自动清理", self.keep_spin)

        body.addWidget(_title_label("默认指令"))
        self.instruction_edit = LineEdit()
        self.instruction_edit.setPlaceholderText("例如：保持简洁；面向开发者；保留 Markdown 结构")
        self.instruction_edit.setClearButtonEnabled(True)
        body.addWidget(self.instruction_edit)

        self.stream_switch.checkedChanged.connect(
            lambda value: self._save("stream", bool(value))
        )
        self.glossary_switch.checkedChanged.connect(
            lambda value: self._save("use_glossary", bool(value))
        )
        self.keep_spin.valueChanged.connect(
            lambda value: self._save("keep_history", int(value))
        )
        self.instruction_edit.editingFinished.connect(self._save_instruction)
        return card

    def _build_core_card(self) -> Card:
        """翻译底座的用户可调项：缓存 / 限流 / 自动降级。"""
        card, body = self._card("翻译底座")
        intro = MutedLabel(
            "主窗口、划词浮窗、框选截屏和批量队列共用同一条管道："
            "同样的原文第二次出现会直接命中缓存，限流保证不会把免费接口打爆，"
            "遇到 429 会自动换更小的模型。",
            size=11,
        )
        intro.setWordWrap(True)
        body.addWidget(intro)

        self.core_cache_switch = SwitchButton()
        self._row(body, "结果缓存", "相同原文与参数直接复用译文，不占网络", self.core_cache_switch)

        self.core_ttl_spin = SpinBox()
        self.core_ttl_spin.setRange(1, 90)
        self.core_ttl_spin.setSuffix(" 天")
        self.core_ttl_spin.setMinimumWidth(160)
        self._row(body, "缓存有效期", "过期后重新请求；命中的条目会刷新时间", self.core_ttl_spin)

        self.core_fallback_switch = SwitchButton()
        self._row(
            body,
            "自动降级",
            "超时 / 429 / 5xx 时依次尝试 9B、2B；已经流出译文则不再改模型",
            self.core_fallback_switch,
        )

        self.core_conc_spin = SpinBox()
        self.core_conc_spin.setRange(1, 8)
        self.core_conc_spin.setSuffix(" 个")
        self.core_conc_spin.setMinimumWidth(160)
        self._row(body, "并发上限", "同时进行的请求数，批量任务也受它约束", self.core_conc_spin)

        self.core_gap_spin = DoubleSpinBox()
        self.core_gap_spin.setRange(0.0, 3.0)
        self.core_gap_spin.setSingleStep(0.05)
        self.core_gap_spin.setDecimals(2)
        self.core_gap_spin.setSuffix(" 秒")
        self.core_gap_spin.setMinimumWidth(160)
        self._row(body, "请求最小间隔", "两次请求之间至少隔这么久，0 表示不限", self.core_gap_spin)

        self.core_cache_switch.checkedChanged.connect(
            lambda value: self._save("cache_enabled", bool(value))
        )
        self.core_ttl_spin.valueChanged.connect(
            lambda value: self._save("cache_ttl_days", int(value))
        )
        self.core_fallback_switch.checkedChanged.connect(
            lambda value: self._save("fallback_enabled", bool(value))
        )
        self.core_conc_spin.valueChanged.connect(
            lambda value: self._save("rate_max_concurrency", int(value))
        )
        self.core_gap_spin.valueChanged.connect(
            lambda value: self._save("rate_min_interval", round(float(value), 2))
        )
        return card

    def _build_appearance_card(self) -> Card:
        card, body = self._card("外观")
        self.theme_combo = ComboBox()
        self.theme_combo.setMinimumWidth(220)
        for value, label in THEMES:
            self.theme_combo.addItem(label, userData=value)
        self._row(body, "主题", "跟随系统时会随 Windows 深浅色切换", self.theme_combo)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)

        self.mica_switch = SwitchButton()
        self.mica_switch.setOnText("开")
        self.mica_switch.setOffText("关")
        self.mica_switch.setMinimumWidth(220)
        self._row(
            body,
            "云母背景（Mica）",
            "Windows 11 上给窗口加半透明材质；Windows 10 会自动忽略",
            self.mica_switch,
        )
        self.mica_switch.checkedChanged.connect(self._on_mica_toggled)

        self.language_combo = ComboBox()
        self.language_combo.setMinimumWidth(220)
        for code, label in i18n.available():
            self.language_combo.addItem(label, userData=code)
        self._row(
            body,
            "界面语言",
            "只影响导航与主要按钮文案，错误提示保持中文",
            self.language_combo,
        )
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        return card

    def _build_sync_card(self) -> Card:
        card, body = self._card("同步与更新")
        body.addWidget(
            MutedLabel(
                "只同步术语、风格预设与偏好；译文缓存和批量任务留在本机，避免把绝对路径带到另一台电脑。",
                size=11,
            )
        )

        self.sync_url_edit = LineEdit()
        self.sync_url_edit.setPlaceholderText("https://dav.jianguoyun.com/dav/IndexTranslate")
        self.sync_url_edit.setMinimumWidth(320)
        self.sync_url_edit.editingFinished.connect(self._save_sync)
        self._row(body, "WebDAV 地址", "坚果云 / Nextcloud / Alist 的目录地址", self.sync_url_edit)

        self.sync_user_edit = LineEdit()
        self.sync_user_edit.setPlaceholderText("账号 / 邮箱")
        self.sync_user_edit.setMinimumWidth(320)
        self.sync_user_edit.editingFinished.connect(self._save_sync)
        self._row(body, "账号", "", self.sync_user_edit)

        self.sync_password_edit = PasswordLineEdit()
        self.sync_password_edit.setPlaceholderText("留空表示不修改已保存的密码")
        self.sync_password_edit.setMinimumWidth(320)
        self.sync_password_edit.editingFinished.connect(self._save_sync)
        self._row(
            body,
            "密码 / 应用密码",
            "用 Windows DPAPI 加密保存，只在当前账户可解密",
            self.sync_password_edit,
        )

        self.sync_auto_switch = SwitchButton()
        self.sync_auto_switch.setOnText("开")
        self.sync_auto_switch.setOffText("关")
        self.sync_auto_switch.setMinimumWidth(220)
        self._row(body, "启动时自动同步", "启动后台拉取一次并回推本地改动", self.sync_auto_switch)
        self.sync_auto_switch.checkedChanged.connect(
            lambda checked: self._save("sync_auto", bool(checked))
        )

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.sync_test_button = PushButton("测试连接")
        self.sync_now_button = PrimaryPushButton("立即同步")
        self.sync_test_button.setFixedHeight(34)
        self.sync_now_button.setFixedHeight(34)
        self.sync_test_button.clicked.connect(self._test_sync)
        self.sync_now_button.clicked.connect(self._sync_now)
        self.sync_state = MutedLabel("尚未同步", size=11)
        actions.addWidget(self.sync_test_button)
        actions.addWidget(self.sync_now_button)
        actions.addWidget(self.sync_state, 1)
        body.addLayout(actions)
        return card

    def _build_update_card(self) -> Card:
        card, body = self._card("软件更新")
        body.addWidget(
            MutedLabel(
                "读 GitHub Releases 判断新旧：后台下载并校验 SHA-256，"
                "再由临时脚本替换、自动重启。",
                size=11,
            )
        )

        info = QHBoxLayout()
        info.setSpacing(8)
        info.addWidget(_title_label("当前版本"))
        self.update_version_label = MutedLabel(VERSION, size=12)
        info.addWidget(self.update_version_label)
        info.addStretch(1)
        self.update_page_button = PushButton("打开发布页")
        self.update_page_button.setFixedHeight(34)
        self.update_page_button.setToolTip("手动下载：GitHub 接口限流时也能自己拿包")
        self.update_page_button.clicked.connect(self._open_releases_page)
        info.addWidget(self.update_page_button)
        self.update_check_button = PushButton("检查更新")
        self.update_check_button.setFixedHeight(34)
        self.update_check_button.clicked.connect(self._check_update)
        info.addWidget(self.update_check_button)
        body.addLayout(info)

        auto = QHBoxLayout()
        auto.setSpacing(8)
        title = QVBoxLayout()
        title.setSpacing(2)
        title.addWidget(_title_label("启动时自动检查"))
        title.addWidget(MutedLabel("启动 6 秒后在后台查一次，不打扰使用", size=11))
        auto.addLayout(title)
        auto.addStretch(1)
        self.update_auto_switch = SwitchButton()
        self.update_auto_switch.setToolTip("启动后自动检查一次新版本")
        self.update_auto_switch.setOnText("")
        self.update_auto_switch.setOffText("")
        self.update_auto_switch.checkedChanged.connect(self._on_update_auto_changed)
        auto.addWidget(self.update_auto_switch)
        body.addLayout(auto)

        self.update_state = MutedLabel("", size=11)
        body.addWidget(self.update_state)
        self.update_progress = ProgressBar()
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(0)
        self.update_progress.setVisible(False)
        body.addWidget(self.update_progress)
        return card

    def _build_system_card(self) -> Card:
        card, body = self._card("系统集成")
        body.addWidget(
            MutedLabel(
                "常驻托盘后，关掉窗口本地代理也不会断，全局快捷键随时可用。",
                size=11,
            )
        )

        self.tray_switch = SwitchButton()
        self._row(
            body,
            "关闭窗口时最小化到托盘",
            "关闭后程序继续在后台运行，单击托盘图标可回到主窗口",
            self.tray_switch,
        )

        self.autostart_switch = SwitchButton()
        self._row(
            body,
            "开机静默自启",
            "登录 Windows 后直接进托盘，不弹出主窗口",
            self.autostart_switch,
        )

        self.autostart_proxy_switch = SwitchButton()
        self._row(
            body,
            "启动时自动开启本地代理",
            "让沉浸式翻译等外部工具随时可用，无需手动点「启动」",
            self.autostart_proxy_switch,
        )

        self.tray_switch.checkedChanged.connect(
            lambda value: self._save("minimize_to_tray", bool(value))
        )
        self.autostart_proxy_switch.checkedChanged.connect(
            lambda value: self._save("autostart_proxy", bool(value))
        )
        self.autostart_switch.checkedChanged.connect(self._on_autostart_toggled)

        self.popup_switch = SwitchButton()
        self._row(
            body,
            "全局划词浮窗",
            "在任意程序里选中文字，按快捷键即可在鼠标旁弹出译文",
            self.popup_switch,
        )

        hotkey_row = QHBoxLayout()
        hotkey_row.setSpacing(8)
        self.hotkey_edit = LineEdit()
        self.hotkey_edit.setPlaceholderText("例如 Ctrl+Alt+T")
        self.hotkey_edit.setClearButtonEnabled(True)
        self.hotkey_edit.setFixedHeight(34)
        self.hotkey_apply = PushButton("应用快捷键")
        self.hotkey_apply.setMinimumWidth(112)
        self.hotkey_apply.setFixedHeight(34)
        hotkey_row.addWidget(_title_label("划词快捷键"))
        hotkey_row.addWidget(self.hotkey_edit, 1)
        hotkey_row.addWidget(self.hotkey_apply)
        body.addLayout(hotkey_row)

        self.hotkey_state = MutedLabel("", size=11)
        body.addWidget(self.hotkey_state)

        self.popup_switch.checkedChanged.connect(self._on_popup_toggled)
        self.hotkey_apply.clicked.connect(self._apply_hotkey)
        self.hotkey_edit.returnPressed.connect(self._apply_hotkey)

        shot_row = QHBoxLayout()
        shot_row.setSpacing(8)
        self.shot_hotkey_edit = LineEdit()
        self.shot_hotkey_edit.setPlaceholderText("例如 Ctrl+Alt+S")
        self.shot_hotkey_edit.setClearButtonEnabled(True)
        self.shot_hotkey_edit.setFixedHeight(34)
        self.shot_hotkey_apply = PushButton("应用快捷键")
        self.shot_hotkey_apply.setMinimumWidth(112)
        self.shot_hotkey_apply.setFixedHeight(34)
        shot_row.addWidget(_title_label("截屏快捷键"))
        shot_row.addWidget(self.shot_hotkey_edit, 1)
        shot_row.addWidget(self.shot_hotkey_apply)
        body.addLayout(shot_row)

        self.shot_hotkey_state = MutedLabel("", size=11)
        body.addWidget(self.shot_hotkey_state)

        self.shot_hotkey_apply.clicked.connect(self._apply_shot_hotkey)
        self.shot_hotkey_edit.returnPressed.connect(self._apply_shot_hotkey)

        self.capture_target_combo = ComboBox()
        self.capture_target_combo.addItem("主窗口", userData="main")
        self.capture_target_combo.addItem("浮窗（不切窗口）", userData="popup")
        self.capture_target_combo.setMinimumWidth(220)
        self._row(
            body,
            "截屏结果显示",
            "选「浮窗」后框选完直接在鼠标旁出译文，主窗口不会被拉到前面",
            self.capture_target_combo,
        )
        self.capture_target_combo.currentIndexChanged.connect(
            lambda _: self._save("capture_target", self.capture_target_combo.currentData() or "main")
        )

        self.replace_switch = SwitchButton()
        self._row(
            body,
            "输入框原地替换",
            "取选区（没有选区就全选）→ 翻译 → 把译文粘回原位置，剪贴板不受影响",
            self.replace_switch,
        )

        replace_row = QHBoxLayout()
        replace_row.setSpacing(8)
        self.replace_hotkey_edit = LineEdit()
        self.replace_hotkey_edit.setPlaceholderText("例如 Ctrl+Alt+R")
        self.replace_hotkey_edit.setClearButtonEnabled(True)
        self.replace_hotkey_edit.setFixedHeight(34)
        self.replace_hotkey_apply = PushButton("应用快捷键")
        self.replace_hotkey_apply.setMinimumWidth(112)
        self.replace_hotkey_apply.setFixedHeight(34)
        replace_row.addWidget(_title_label("替换快捷键"))
        replace_row.addWidget(self.replace_hotkey_edit, 1)
        replace_row.addWidget(self.replace_hotkey_apply)
        body.addLayout(replace_row)

        self.replace_hotkey_state = MutedLabel("", size=11)
        body.addWidget(self.replace_hotkey_state)

        self.replace_switch.checkedChanged.connect(self._on_replace_toggled)
        self.replace_hotkey_apply.clicked.connect(self._apply_replace_hotkey)
        self.replace_hotkey_edit.returnPressed.connect(self._apply_replace_hotkey)
        return card

    def _on_replace_toggled(self, checked: bool) -> None:
        self._save("replace_enabled", bool(checked))
        self.replaceHotkeyChanged.emit(
            str(settings.get("replace_hotkey", "")) if checked else ""
        )

    def _apply_replace_hotkey(self) -> None:
        sequence = self.replace_hotkey_edit.text().strip()
        if not sequence:
            InfoBar.warning(
                title="快捷键不能为空",
                content="例如 Ctrl+Alt+R。",
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            return
        self._save("replace_hotkey", sequence)
        self.replaceHotkeyChanged.emit(sequence)

    def report_replace_hotkey(self, ok: bool, detail: str) -> None:
        if ok:
            self.replace_hotkey_edit.setText(detail)
            self.replace_hotkey_state.setText(f"已生效：{detail}")
        else:
            self.replace_hotkey_state.setText(detail)

    def _on_popup_toggled(self, checked: bool) -> None:
        self._save("popup_enabled", bool(checked))
        self.hotkeyChanged.emit(str(settings.get("popup_hotkey", "")) if checked else "")

    def _apply_hotkey(self) -> None:
        sequence = self.hotkey_edit.text().strip()
        if not sequence:
            InfoBar.warning(
                title="快捷键不能为空",
                content="例如 Ctrl+Alt+T。",
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            return
        self._save("popup_hotkey", sequence)
        self.hotkeyChanged.emit(sequence)

    def report_hotkey(self, ok: bool, detail: str) -> None:
        """MainWindow 注册完成后回写状态，避免界面显示"已生效"其实没注册上。"""
        if ok:
            self.hotkey_edit.setText(detail)
            self.hotkey_state.setText(f"已生效：{detail}")
        else:
            self.hotkey_state.setText(detail)

    def _apply_shot_hotkey(self) -> None:
        sequence = self.shot_hotkey_edit.text().strip()
        if not sequence:
            InfoBar.warning(
                title="快捷键不能为空",
                content="例如 Ctrl+Alt+S。",
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            return
        self._save("shot_hotkey", sequence)
        self.shotHotkeyChanged.emit(sequence)

    def report_shot_hotkey(self, ok: bool, detail: str) -> None:
        if ok:
            self.shot_hotkey_edit.setText(detail)
            self.shot_hotkey_state.setText(f"已生效：{detail}")
        else:
            self.shot_hotkey_state.setText(detail)

    def _build_proxy_card(self) -> Card:
        card, body = self._card("本地代理（供沉浸式翻译等工具使用）")
        body.addWidget(
            MutedLabel(
                "开启后，把其他翻译工具的 OpenAI 接口地址设为下面的地址即可复用本应用的服务。",
                size=11,
            )
        )

        port_row = QHBoxLayout()
        port_row.setSpacing(8)
        self.port_spin = SpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setMinimumWidth(140)
        self.proxy_button = PrimaryPushButton("启动")
        self.proxy_button.setMinimumWidth(96)
        port_row.addWidget(_title_label("端口"))
        port_row.addWidget(self.port_spin)
        port_row.addStretch(1)
        port_row.addWidget(self.proxy_button)
        body.addLayout(port_row)

        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        self.proxy_url_edit = LineEdit()
        self.proxy_url_edit.setReadOnly(True)
        self.proxy_url_edit.setPlaceholderText("未启动")
        self.proxy_copy_button = PushButton("复制地址")
        self.proxy_copy_button.setMinimumWidth(104)
        self.proxy_copy_button.setEnabled(False)
        url_row.addWidget(self.proxy_url_edit, 1)
        url_row.addWidget(self.proxy_copy_button)
        body.addLayout(url_row)

        self.proxy_state = MutedLabel("未启动", size=11)
        body.addWidget(self.proxy_state)

        self.port_spin.valueChanged.connect(self._on_port_changed)
        self.proxy_button.clicked.connect(self._toggle_proxy)
        self.proxy_copy_button.clicked.connect(self._copy_proxy_url)
        return card

    def _build_proxy_optimize_card(self) -> Card:
        card, body = self._card("代理优化（限流 · 缓存 · 术语 · 降级）")
        body.addWidget(
            MutedLabel(
                "沉浸式翻译打开长网页会瞬间发出几十个请求，最容易触发公网限流。"
                "这里的限流与缓存就是为了把这种流量削平。",
                size=11,
            )
        )

        self.concurrency_spin = SpinBox()
        self.concurrency_spin.setRange(1, 32)
        self.concurrency_spin.setMinimumWidth(140)
        self._row(body, "最大并发", "同时在途的上游请求数，越小越稳", self.concurrency_spin)

        self.qps_spin = DoubleSpinBox()
        self.qps_spin.setRange(0.0, 30.0)
        self.qps_spin.setSingleStep(0.5)
        self.qps_spin.setDecimals(1)
        self.qps_spin.setMinimumWidth(140)
        self._row(body, "每秒请求", "令牌桶速率，0 表示不限速", self.qps_spin)

        cache_box = QWidget()
        cache_row = QHBoxLayout(cache_box)
        cache_row.setContentsMargins(0, 0, 0, 0)
        cache_row.setSpacing(10)
        self.cache_switch = SwitchButton()
        self.cache_switch.setOnText("开")
        self.cache_switch.setOffText("关")
        self.cache_size_spin = SpinBox()
        self.cache_size_spin.setRange(0, 50000)
        self.cache_size_spin.setSingleStep(500)
        self.cache_size_spin.setSuffix(" 条")
        self.cache_size_spin.setMinimumWidth(140)
        self.cache_clear_button = PushButton("清空")
        self.cache_clear_button.setMinimumWidth(76)
        cache_row.addWidget(self.cache_switch)
        cache_row.addWidget(self.cache_size_spin)
        cache_row.addWidget(self.cache_clear_button)
        cache_row.addStretch(1)
        self._row(
            body,
            "结果缓存",
            "原文与参数都相同时直接命中，网页刷新后再也不打上游",
            cache_box,
        )
        self.cache_state = MutedLabel("缓存未启用", size=11)
        body.addWidget(self.cache_state)

        self.glossary_inject_switch = SwitchButton()
        self.glossary_inject_switch.setOnText("开")
        self.glossary_inject_switch.setOffText("关")
        self._row(
            body,
            "注入术语表",
            "命中术语表的词条会自动加进发往模型的提示词",
            self.glossary_inject_switch,
        )

        self.fallback_switch = SwitchButton()
        self.fallback_switch.setOnText("开")
        self.fallback_switch.setOffText("关")
        self._row(
            body,
            "模型自动降级",
            "35B 遇到 429 / 5xx / 超时时自动改用 9B，再退到 2B",
            self.fallback_switch,
        )

        self.fallback_base_edit = LineEdit()
        self.fallback_base_edit.setPlaceholderText("http://127.0.0.1:11434/v1")
        self._row(
            body,
            "备用端点",
            "留空则只走内置降级；可填 Ollama / 硅基流动 / DeepSeek 的接口地址",
            self.fallback_base_edit,
        )

        self.fallback_model_edit = LineEdit()
        self.fallback_model_edit.setPlaceholderText("qwen2.5:7b / deepseek-chat")
        self._row(body, "备用模型", "调用备用端点时使用的模型名", self.fallback_model_edit)

        self.fallback_key_edit = PasswordLineEdit()
        self.fallback_key_edit.setPlaceholderText("本地 Ollama 可留空")
        self._row(body, "备用密钥", "以 Authorization: Bearer 发送，仅保存在本机 settings.json", self.fallback_key_edit)

        self.concurrency_spin.valueChanged.connect(
            lambda value: self._save("proxy_max_concurrency", int(value))
        )
        self.qps_spin.valueChanged.connect(lambda value: self._save("proxy_qps", float(value)))
        self.cache_switch.checkedChanged.connect(self._on_cache_toggled)
        self.cache_size_spin.valueChanged.connect(self._on_cache_size_changed)
        self.cache_clear_button.clicked.connect(self._clear_cache)
        self.glossary_inject_switch.checkedChanged.connect(
            lambda value: self._save("proxy_inject_glossary", bool(value))
        )
        self.fallback_switch.checkedChanged.connect(
            lambda value: self._save("proxy_fallback", bool(value))
        )
        for edit, key, strip in (
            (self.fallback_base_edit, "fallback_api_base", True),
            (self.fallback_model_edit, "fallback_model", True),
            (self.fallback_key_edit, "fallback_api_key", False),
        ):
            edit.editingFinished.connect(
                lambda e=edit, k=key, s=strip: self._save(k, e.text().strip() if s else e.text())
            )
        return card

    def _build_about_card(self) -> Card:
        card, body = self._card("关于")
        body.addWidget(MutedLabel(f"{APP_PROJECT} {VERSION} · 本地运行的翻译客户端", size=12))
        body.addWidget(MutedLabel(f"数据目录：{data_dir()}", size=11))

        row = QHBoxLayout()
        row.setSpacing(8)
        open_button = PushButton("打开数据目录")
        open_button.clicked.connect(self._open_data_dir)
        row.addWidget(open_button)
        row.addWidget(HyperlinkButton(SERVICE_HOME, "服务主页"))
        row.addStretch(1)
        body.addLayout(row)

        # 官方分发渠道：软件里必须留一个能核对来源的入口，否则改包伪冒无从辨认。
        official_row = QHBoxLayout()
        official_row.setSpacing(8)
        self.github_button = PrimaryPushButton(FluentIcon.GITHUB, "官方 GitHub 仓库")
        self.github_button.setToolTip(PROJECT_URL)
        self.github_button.clicked.connect(self._open_project_page)
        releases_button = PushButton("下载与发行版")
        releases_button.clicked.connect(self._open_releases_page)
        official_row.addWidget(self.github_button)
        official_row.addWidget(releases_button)
        official_row.addStretch(1)
        body.addLayout(official_row)
        body.addWidget(
            MutedLabel(
                f"仅此一个官方渠道：{PROJECT_URL}\n"
                "本软件没有任何其他下载站点；遇到同名软件请核对本页地址与版本号，"
                "改包版本常伴随后门或广告注入。",
                size=11,
            )
        )

        reset_button = PushButton("恢复默认设置")
        reset_button.clicked.connect(self._reset_settings)
        body.addWidget(reset_button, 0, Qt.AlignmentFlag.AlignLeft)
        return card

    # -------------------------------------------------------------------- sync
    def _sync_from_settings(self) -> None:
        self.api_edit.setText(str(settings.get("api_base", API_BASE)))
        self._select_data(self.model_combo, settings.get("model"))
        self.timeout_spin.setValue(int(settings.get("timeout", 120) or 120))
        self.stream_switch.setChecked(bool(settings.get("stream", True)))
        self.glossary_switch.setChecked(bool(settings.get("use_glossary", True)))
        self.keep_spin.setValue(int(settings.get("keep_history", 500) or 0))
        self.instruction_edit.setText(str(settings.get("instruction", "") or ""))
        self.core_cache_switch.setChecked(bool(settings.get("cache_enabled", True)))
        self.core_ttl_spin.setValue(int(settings.get("cache_ttl_days", 7) or 7))
        self.core_fallback_switch.setChecked(bool(settings.get("fallback_enabled", True)))
        self.core_conc_spin.setValue(int(settings.get("rate_max_concurrency", 3) or 3))
        self.core_gap_spin.setValue(float(settings.get("rate_min_interval", 0.25) or 0.0))
        self._select_data(self.theme_combo, settings.get("theme", "auto"))
        self.port_spin.setValue(int(settings.get("proxy_port", 8080) or 8080))
        self.tray_switch.setChecked(bool(settings.get("minimize_to_tray", True)))
        self.autostart_switch.setChecked(autostart_enabled())
        self.autostart_proxy_switch.setChecked(bool(settings.get("autostart_proxy", True)))
        self.popup_switch.setChecked(bool(settings.get("popup_enabled", True)))
        self.hotkey_edit.setText(str(settings.get("popup_hotkey", "Ctrl+Alt+T") or ""))
        self.shot_hotkey_edit.setText(str(settings.get("shot_hotkey", "Ctrl+Alt+S") or ""))
        self._select_data(self.capture_target_combo, settings.get("capture_target", "main"))
        self.replace_switch.setChecked(bool(settings.get("replace_enabled", True)))
        self.replace_hotkey_edit.setText(str(settings.get("replace_hotkey", "Ctrl+Alt+R") or ""))
        self.concurrency_spin.setValue(int(settings.get("proxy_max_concurrency", 4) or 1))
        self.qps_spin.setValue(float(settings.get("proxy_qps", 3.0) or 0.0))
        cache_on = bool(settings.get("proxy_cache", True))
        self.cache_switch.setChecked(cache_on)
        self.cache_size_spin.setValue(int(settings.get("proxy_cache_size", 2000) or 0))
        self.cache_size_spin.setEnabled(cache_on)
        self.cache_clear_button.setEnabled(cache_on)
        self.glossary_inject_switch.setChecked(bool(settings.get("proxy_inject_glossary", True)))
        self.fallback_switch.setChecked(bool(settings.get("proxy_fallback", True)))
        self.fallback_base_edit.setText(str(settings.get("fallback_api_base", "") or ""))
        self.fallback_model_edit.setText(str(settings.get("fallback_model", "") or ""))
        self.fallback_key_edit.setText(str(settings.get("fallback_api_key", "") or ""))
        self.mica_switch.setChecked(bool(settings.get("mica", True)))
        self.update_auto_switch.setChecked(bool(settings.get("update_check_on_start", True)))
        self._select_data(self.language_combo, settings.get("i18n_lang", i18n.DEFAULT_LANG))
        self.sync_url_edit.setText(str(settings.get("sync_url", "") or ""))
        self.sync_user_edit.setText(str(settings.get("sync_user", "") or ""))
        stored = str(settings.get("sync_password", "") or "")
        self.sync_password_edit.setPlaceholderText(
            "已保存（留空表示不修改）" if stored else "未设置"
        )
        self.sync_auto_switch.setChecked(bool(settings.get("sync_auto", False)))
        last = str(settings.get("sync_last_result", "") or "")
        self.sync_state.setText(last or "尚未同步")
        self.sync_proxy_state()

    @staticmethod
    def _select_data(combo: ComboBox, value) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def reload(self) -> None:
        self._sync_from_settings()

    def _save(self, key: str, value) -> None:
        settings.set(key, value)
        settings.save()
        if key.startswith("proxy_") or key.startswith("fallback_"):
            # 限流/缓存/降级都是热更新，不必重启代理
            self._proxy_manager.apply_settings()
            self.refresh_proxy_stats()
        self.settingsChanged.emit()

    # ----------------------------------------------------------------- handlers
    def _save_api_base(self) -> None:
        value = self.api_edit.text().strip() or API_BASE
        if not value.startswith(("http://", "https://")):
            InfoBar.warning(
                title="地址无效",
                content="接口地址需要以 http:// 或 https:// 开头。",
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            self.api_edit.setText(str(settings.get("api_base", API_BASE)))
            return
        self._save("api_base", value.rstrip("/"))
        self._proxy_manager.apply_settings()

    def _reset_api_base(self) -> None:
        self.api_edit.setText(API_BASE)
        self._save_api_base()

    def _save_model(self) -> None:
        model = self.model_combo.currentData()
        if not model:
            return
        self._save("model", model)
        self._proxy_manager.apply_settings()

    def _save_instruction(self) -> None:
        self._save("instruction", self.instruction_edit.text().strip())

    def _on_theme_changed(self) -> None:
        value = self.theme_combo.currentData() or "auto"
        self._save("theme", value)
        mapping = {"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}
        setTheme(mapping.get(value, Theme.AUTO), save=True)
        self.themeChanged.emit(value)

    def _on_mica_toggled(self, checked: bool) -> None:
        self._save("mica", bool(checked))
        self.micaChanged.emit(bool(checked))

    def _on_language_changed(self) -> None:
        code = self.language_combo.currentData() or i18n.DEFAULT_LANG
        self._save("i18n_lang", str(code))
        i18n.load(str(code))
        self.languageChanged.emit(str(code))

    # ------------------------------------------------------------ sync / update
    def _save_sync(self) -> None:
        url = self.sync_url_edit.text().strip()
        user = self.sync_user_edit.text().strip()
        changed = url != str(settings.get("sync_url", "")) or user != str(
            settings.get("sync_user", "")
        )
        settings.set("sync_url", url)
        settings.set("sync_user", user)
        settings.set("sync_enabled", bool(url))
        password = self.sync_password_edit.text()
        if password:
            if self._sync_service is not None:
                try:
                    self._sync_service.set_password(password)
                except Exception as exc:  # noqa: BLE001 - 加密失败要说清原因
                    InfoBar.error(
                        title="密码保存失败",
                        content=str(exc),
                        duration=3000,
                        position=InfoBarPosition.TOP_RIGHT,
                        parent=self,
                    )
            self.sync_password_edit.clear()
        if changed:
            self.settingsChanged.emit()
        settings.save()

    def _test_sync(self) -> None:
        if self._sync_service is None:
            self.sync_state.setText("同步服务未初始化")
            return
        self._save_sync()
        ok, message = self._sync_service.test()
        self.sync_state.setText(message)
        (InfoBar.success if ok else InfoBar.error)(
            title="连接正常" if ok else "连接失败",
            content=message,
            duration=3000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _sync_now(self) -> None:
        if self._sync_service is None or self._busy_sync:
            return
        self._save_sync()
        if not str(settings.get("sync_url", "")).strip():
            self.sync_state.setText("请先填写 WebDAV 地址")
            return
        self._busy_sync = True
        self.sync_now_button.setEnabled(False)
        self.sync_state.setText("正在同步…")
        try:
            result = self._sync_service.sync_now()
        finally:
            self._busy_sync = False
            self.sync_now_button.setEnabled(True)
        text = result.message or ("同步完成" if result.ok else "同步失败")
        if result.ok and (result.pushed or result.pulled):
            text = f"{text}（拉取 {result.pulled} 条，推送 {result.pushed} 条）"
        self.sync_state.setText(text)
        (InfoBar.success if result.ok else InfoBar.error)(
            title="同步完成" if result.ok else "同步失败",
            content=text,
            duration=4000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _toast(self, kind, title: str, content: str) -> None:
        """统一的小提示：不抢焦点、几秒后自动消失。"""
        kind(
            title=title,
            content=content,
            duration=4000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _check_update(self, force: bool = True) -> None:
        from ..ui.update_worker import UpdateCheckWorker  # 延迟导入：只有点按钮才需要

        if self._update_worker is not None and self._update_worker.isRunning():
            return
        self.update_state.setText("正在检查…")
        self.update_check_button.setEnabled(False)
        worker = UpdateCheckWorker(
            channel=str(settings.get("update_channel", "stable") or "stable"),
            force=force,
        )
        worker.checked.connect(self._on_update_checked)
        worker.failed.connect(self._on_update_failed)
        worker.finished.connect(self._on_update_check_finished)
        self._update_worker = worker
        worker.start()

    def _on_update_check_finished(self) -> None:
        worker = self._update_worker
        self._update_worker = None
        if worker is not None:
            worker.deleteLater()
        self.update_check_button.setEnabled(True)

    def _on_update_checked(self, status) -> None:
        self.update_state.setText(status.message)
        self._last_status = status
        if not status.ok:
            self._toast(InfoBar.error, "检查失败", status.message)
            return
        release = getattr(status, "release", None)
        if status.available and release is not None:
            self.update_state.setToolTip(str(getattr(release, "notes", ""))[:800])
            self._toast(InfoBar.success, "发现新版本", status.message)
            self._offer_update(release)
            return
        self._toast(InfoBar.success, "已是最新", status.message)

    def _on_update_failed(self, message: str) -> None:
        self.update_state.setText(message + "（可点「打开发布页」手动下载）")
        self._toast(InfoBar.error, "检查更新失败", message)

    def _offer_update(self, release) -> None:
        """发现新版本 → 问一次，愿意就后台下载。"""
        from ..ui.update_worker import install_kind, pick_asset
        from qfluentwidgets import MessageBox

        kind = install_kind()
        asset = pick_asset(release, kind)
        if kind == "source":
            box = MessageBox(
                "发现新版本 {0}".format(release.version),
                "{0}\n\n源码运行模式不能自我替换，请到发布页下载新版。".format(
                    str(getattr(release, "notes", "") or "")[:600]
                ),
                self,
            )
            box.yesButton.setText("去发布页")
            box.cancelButton.setText("知道了")
            if box.exec():
                self._open_releases_page()
            return
        if asset is None:
            self._toast(InfoBar.warning, "缺少安装包", "这个版本没有可下载的 zip / exe。")
            return

        size = asset.size / 1048576 if getattr(asset, "size", 0) else 0
        box = MessageBox(
            "发现新版本 {0}".format(release.version),
            "{0}\n\n当前版本 {1} → 新版本 {2}\n下载 {3}（{4:.1f} MB），"
            "下载完成后会询问是否立即重启安装。".format(
                str(getattr(release, "notes", "") or "")[:600],
                VERSION,
                release.version,
                asset.name,
                size,
            ),
            self,
        )
        box.yesButton.setText("立即下载并更新")
        box.cancelButton.setText("稍后")
        if not box.exec():
            return
        self._start_update_download(release, asset)

    def _start_update_download(self, release, asset) -> None:
        from ..ui.update_worker import UpdateDownloadWorker

        if self._download_worker is not None and self._download_worker.isRunning():
            return
        self.update_progress.setVisible(True)
        self.update_progress.setValue(0)
        self.update_check_button.setEnabled(False)
        self.update_state.setText("正在下载 {0}…".format(asset.name))
        worker = UpdateDownloadWorker(release, asset=asset)
        worker.progressed.connect(self._on_update_progress)
        worker.ready.connect(self._on_update_ready)
        worker.failed.connect(self._on_update_download_failed)
        worker.finished.connect(self._on_update_download_finished)
        self._download_worker = worker
        worker.start()

    def _on_update_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.update_progress.setValue(int(done * 100 / total))
            self.update_state.setText(
                "正在下载… {0:.1f} / {1:.1f} MB".format(done / 1048576, total / 1048576)
            )
        else:
            self.update_state.setText("正在下载… {0:.1f} MB".format(done / 1048576))

    def _on_update_download_finished(self) -> None:
        worker = self._download_worker
        self._download_worker = None
        if worker is not None:
            worker.deleteLater()
        self.update_check_button.setEnabled(True)

    def _on_update_download_failed(self, message: str) -> None:
        self.update_progress.setVisible(False)
        self.update_state.setText(message)
        self._toast(InfoBar.error, "下载更新失败", message)

    def _on_update_ready(self, path, release) -> None:
        from qfluentwidgets import MessageBox

        self.update_progress.setValue(100)
        self.update_state.setText("更新包已就绪：{0}".format(Path(path).name))
        box = MessageBox(
            "更新已下载",
            "{0} 已下载并校验通过。\n\n立即重启完成安装？"
            "（会关闭当前窗口，替换文件后自动重新打开）".format(release.version),
            self,
        )
        box.yesButton.setText("立即重启安装")
        box.cancelButton.setText("稍后")
        if box.exec():
            self._apply_update(path)
        else:
            self._toast(
                InfoBar.info,
                "稍后安装",
                "更新包已保存，下次可以再点「检查更新」安装。",
            )

    def _apply_update(self, path) -> None:
        from PyQt6.QtWidgets import QApplication

        from ..ui.update_worker import build_apply_script, install_kind
        from ..core.updater import Updater

        # 源码运行时 sys.executable 是 python.exe，真按下去会把解释器目录当程序目录替换。
        if install_kind() == "source":
            self.update_state.setText("源码模式不自动替换：请从发布页下载新版，或直接 git pull。")
            self._toast(
                InfoBar.warning,
                "源码模式不自动替换",
                "请到发布页下载新版，或直接 git pull。",
            )
            return

        try:
            script = build_apply_script(Path(path))
            Updater.launch_script(script)
        except Exception as exc:  # noqa: BLE001
            self._toast(InfoBar.error, "无法启动更新脚本", str(exc))
            return

        # 先把托盘、代理、扩展收掉，再退出；批处理会等本进程消失才开始覆盖。
        window = self.window()
        shutdown = getattr(window, "_shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:  # noqa: BLE001
                pass
        QApplication.instance().quit()

    def _auto_check_update(self) -> None:
        if not bool(settings.get("update_check_on_start", True)):
            return
        if not getattr(sys, "frozen", False):
            return
        # 启动时不强制刷新：15 分钟内查过就直接用缓存，免得每次都去撞 GitHub 的限流
        self._check_update(force=False)

    def _on_port_changed(self, value: int) -> None:
        self._save("proxy_port", int(value))
        if self._proxy_manager.running:
            self._proxy_manager.stop()
            self.proxy_state.setText("端口已修改，代理已停止，请重新启动。")

    def _toggle_proxy(self) -> None:
        if self._proxy_manager.running:
            self._proxy_manager.stop()
            return

        if not self._proxy_manager.start():
            return  # 失败提示由 failed 信号统一给出

        InfoBar.success(
            title="代理已启动",
            content="把其他工具的接口地址设为上方地址即可。",
            duration=3000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    # --------------------------------------------------------- proxy feedback
    def _on_proxy_started(self, url: str) -> None:
        self.proxy_button.setText("停止")
        self.proxy_url_edit.setText(url)
        self.proxy_copy_button.setEnabled(True)
        self.proxy_state.setText("运行中：仅监听本机 127.0.0.1，已在请求中固定关闭思考模式。")

    def _on_proxy_stopped(self) -> None:
        self.proxy_button.setText("启动")
        self.proxy_url_edit.clear()
        self.proxy_copy_button.setEnabled(False)
        self.proxy_state.setText("未启动")

    def _on_proxy_failed(self, message: str) -> None:
        self.proxy_state.setText(f"启动失败：{message}")
        InfoBar.error(
            title="代理启动失败",
            content=message,
            duration=5000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def sync_proxy_state(self) -> None:
        """外部（托盘菜单/自动启动）改动代理后刷新界面。"""
        if self._proxy_manager.running:
            self._on_proxy_started(self._proxy_manager.url)
        else:
            self._on_proxy_stopped()
        self.refresh_proxy_stats()

    # --------------------------------------------------- proxy optimisation UI
    def _on_cache_toggled(self, checked: bool) -> None:
        self._save("proxy_cache", bool(checked))
        self.cache_size_spin.setEnabled(bool(checked))
        self.cache_clear_button.setEnabled(bool(checked))
        self.refresh_proxy_stats()

    def _on_cache_size_changed(self, value: int) -> None:
        settings.set("proxy_cache_size", int(value))
        settings.save()
        self._proxy_manager.apply_settings()

    def _clear_cache(self) -> None:
        removed = self._proxy_manager.clear_cache()
        if removed:
            content = f"已清空 {removed} 条缓存。"
        elif self._proxy_manager.running:
            content = "缓存本来就是空的。"
        else:
            content = "代理未启动，缓存随代理一起位于数据目录中。"
        InfoBar.success(
            title="缓存已清空",
            content=content,
            duration=2500,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )
        self.refresh_proxy_stats()

    def refresh_proxy_stats(self) -> None:
        """把真实运行数据回填到缓存状态行（只在代理运行时）。"""
        if not hasattr(self, "cache_state"):
            return
        if not self._proxy_manager.running:
            self.cache_state.setText(
                "代理未启动：启动后可查看命中统计。" if settings.get("proxy_cache", True) else "缓存已关闭"
            )
            return
        stats = self._proxy_manager.stats() or {}
        cache = stats.get("cache") or {}
        gate = stats.get("gate") or {}
        lookups = int(cache.get("lookups", 0) or 0)
        hits = int(cache.get("hits", 0) or 0)
        rate = (hits / lookups * 100.0) if lookups else 0.0
        entries = int(cache.get("entries", 0) or 0)
        served = int(gate.get("served", 0) or 0)
        rejected = int(gate.get("rejected", 0) or 0)
        self.cache_state.setText(
            f"缓存 {entries} 条 · 命中 {hits}/{lookups}（{rate:.0f}%） · "
            f"已处理 {served} 个请求 · 排队超时 {rejected} 次"
        )

    def _on_autostart_toggled(self, checked: bool) -> None:
        try:
            set_autostart(bool(checked))
        except AutostartError as exc:
            InfoBar.error(
                title="开机自启设置失败",
                content=str(exc),
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            self.autostart_switch.setChecked(autostart_enabled())
            return
        self._save("autostart", bool(checked))
        InfoBar.success(
            title="已开启开机自启" if checked else "已关闭开机自启",
            content="登录 Windows 后会静默启动到托盘。" if checked else "下次登录不再自动启动。",
            duration=2400,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _copy_proxy_url(self) -> None:
        url = self.proxy_url_edit.text().strip()
        if not url:
            return
        QGuiApplication.clipboard().setText(url)
        InfoBar.success(
            title="已复制",
            content=url,
            duration=1800,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def _open_project_page(self) -> None:
        self._open_url(PROJECT_URL, "无法打开浏览器")

    def _open_releases_page(self) -> None:
        self._open_url(PROJECT_RELEASES_URL, "无法打开浏览器")

    def _open_url(self, url: str, error_title: str) -> None:
        if not QDesktopServices.openUrl(QUrl(url)):
            InfoBar.error(
                title=error_title,
                content=f"请手动访问：{url}",
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )

    def _open_data_dir(self) -> None:
        path = Path(data_dir())
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            InfoBar.error(
                title="无法打开目录",
                content=f"{path}（{exc}）",
                duration=4000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )

    def _reset_settings(self) -> None:
        from ..core.config import DEFAULTS

        for key, value in DEFAULTS.items():
            if key in ("window_geometry", "window_state"):
                continue
            settings.set(key, value)
        settings.save()
        self._sync_from_settings()
        self._proxy_manager.apply_settings()
        self.refresh_proxy_stats()
        self.settingsChanged.emit()
        self.themeChanged.emit(str(settings.get("theme", "auto")))
        InfoBar.success(
            title="已恢复默认",
            content="除窗口位置外的设置已重置。",
            duration=2600,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    # ------------------------------------------------------------------ cleanup
    def shutdown(self) -> None:
        # 代理由 MainWindow 持有并在退出时统一停止，这里只重置展示状态。
        self.proxy_url_edit.clear()
        self.proxy_copy_button.setEnabled(False)
        if self._sync_service is not None:
            try:
                self._sync_service.close()
            except Exception:  # noqa: BLE001 - 退出路径不该再抛
                pass
