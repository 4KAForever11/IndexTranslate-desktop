"""Settings: service, translation behaviour, appearance, local proxy, about."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox,
    DoubleSpinBox,
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    SpinBox,
    SwitchButton,
    Theme,
    TitleLabel,
    setTheme,
)

from ..core import settings
from ..core.autostart import AutostartError, is_enabled as autostart_enabled
from ..core.autostart import set_enabled as set_autostart
from ..core.config import API_BASE, APP_PROJECT, MODELS, SERVICE_HOME, VERSION, data_dir
from ..core.proxy_manager import ProxyManager
from .theme import Card, MutedLabel, PageWidget, TextLabel

THEMES = [("auto", "跟随系统"), ("light", "浅色"), ("dark", "深色")]


def _title_label(text: str) -> QLabel:
    return TextLabel(text, size=13, bold=True)


class SettingsPage(PageWidget):
    themeChanged = pyqtSignal(str)
    settingsChanged = pyqtSignal()
    hotkeyChanged = pyqtSignal(str)
    shotHotkeyChanged = pyqtSignal(str)

    def __init__(self, proxy_manager: ProxyManager, parent=None) -> None:
        super().__init__(parent)
        # 代理的所有权在 MainWindow，设置页只驱动它并展示状态。
        self._proxy_manager = proxy_manager
        self._build_ui()
        self._sync_from_settings()
        self._proxy_manager.started.connect(self._on_proxy_started)
        self._proxy_manager.stopped.connect(self._on_proxy_stopped)
        self._proxy_manager.failed.connect(self._on_proxy_failed)

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
        layout.addWidget(self._build_appearance_card())
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

    def _build_appearance_card(self) -> Card:
        card, body = self._card("外观")
        self.theme_combo = ComboBox()
        self.theme_combo.setMinimumWidth(220)
        for value, label in THEMES:
            self.theme_combo.addItem(label, userData=value)
        self._row(body, "主题", "跟随系统时会随 Windows 深浅色切换", self.theme_combo)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
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
        return card

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
        self._select_data(self.theme_combo, settings.get("theme", "auto"))
        self.port_spin.setValue(int(settings.get("proxy_port", 8080) or 8080))
        self.tray_switch.setChecked(bool(settings.get("minimize_to_tray", True)))
        self.autostart_switch.setChecked(autostart_enabled())
        self.autostart_proxy_switch.setChecked(bool(settings.get("autostart_proxy", True)))
        self.popup_switch.setChecked(bool(settings.get("popup_enabled", True)))
        self.hotkey_edit.setText(str(settings.get("popup_hotkey", "Ctrl+Alt+T") or ""))
        self.shot_hotkey_edit.setText(str(settings.get("shot_hotkey", "Ctrl+Alt+S") or ""))
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
