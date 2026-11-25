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
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    LineEdit,
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
from ..core.config import API_BASE, APP_PROJECT, MODELS, SERVICE_HOME, VERSION, data_dir
from ..core.proxy_server import ProxyServer
from .theme import Card, MutedLabel, PageWidget, TextLabel

THEMES = [("auto", "跟随系统"), ("light", "浅色"), ("dark", "深色")]


def _title_label(text: str) -> QLabel:
    return TextLabel(text, size=13, bold=True)


class SettingsPage(PageWidget):
    themeChanged = pyqtSignal(str)
    settingsChanged = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._proxy: ProxyServer | None = None
        self._build_ui()
        self._sync_from_settings()

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
        layout.addWidget(self._build_proxy_card())
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
        if self._proxy is not None and self._proxy.running:
            self._proxy.update(api_base=value.rstrip("/"))

    def _reset_api_base(self) -> None:
        self.api_edit.setText(API_BASE)
        self._save_api_base()

    def _save_model(self) -> None:
        model = self.model_combo.currentData()
        if not model:
            return
        self._save("model", model)
        if self._proxy is not None and self._proxy.running:
            self._proxy.update(model=model)

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
        if self._proxy is not None and self._proxy.running:
            self._proxy.stop()
            self._proxy = None
            self.proxy_button.setText("启动")
            self.proxy_url_edit.clear()
            self.proxy_copy_button.setEnabled(False)
            self.proxy_state.setText("端口已修改，代理已停止，请重新启动。")

    def _toggle_proxy(self) -> None:
        if self._proxy is not None and self._proxy.running:
            self._proxy.stop()
            self._proxy = None
            self.proxy_button.setText("启动")
            self.proxy_copy_button.setEnabled(False)
            self.proxy_state.setText("未启动")
            self.proxy_url_edit.clear()
            return

        proxy = ProxyServer(
            port=int(self.port_spin.value()),
            api_base=str(settings.get("api_base", API_BASE)),
            model=str(settings.get("model", "")),
            timeout=float(settings.get("timeout", 120) or 120),
        )
        try:
            url = proxy.start()
        except RuntimeError as exc:
            InfoBar.error(
                title="代理启动失败",
                content=str(exc),
                duration=5000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            return

        self._proxy = proxy
        self.proxy_button.setText("停止")
        self.proxy_url_edit.setText(url)
        self.proxy_copy_button.setEnabled(True)
        self.proxy_state.setText("运行中：仅监听本机 127.0.0.1，已在请求中固定关闭思考模式。")
        InfoBar.success(
            title="代理已启动",
            content="把其他工具的接口地址设为上方地址即可。",
            duration=3000,
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
        if self._proxy is not None:
            self._proxy.stop()
            self._proxy = None
