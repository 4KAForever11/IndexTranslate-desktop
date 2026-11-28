"""The main translation workspace: text or image in, streamed translation out."""

from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage, QKeySequence, QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    TogglePushButton,
    StrongBodyLabel,
    TextBrowser,
    ToolButton,
)

from ..core import settings
from ..core.config import DEFAULT_MODEL, MAX_INPUT_CHARS, MODELS
from ..core.glossary_db import GlossaryDB
from ..core.history_db import HistoryDB
from ..core.image_utils import SUPPORTED_FILTER, human_size
from ..core.styles import find_style, load_styles, style_text
from ..workers.translation_worker import TranslationRequest, TranslationWorker
from .theme import (
    Card,
    Divider,
    MutedLabel,
    PageWidget,
    StatusDot,
    colors,
    on_theme_changed,
)
from .capture_overlay import CaptureOverlay
from .widgets import (
    IMAGE_SUFFIXES,
    ImageDropArea,
    StyleChipBar,
    StyleManagerDialog,
    UrlInputDialog,
)

EDITOR_QSS = """
QPlainTextEdit, QTextBrowser {{
    background: transparent;
    border: none;
    padding: 4px 6px;
    color: {text};
}}
QPlainTextEdit:focus, QTextBrowser:focus {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {handle}; border-radius: 4px; min-height: 28px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""

# 草稿只保留最近的这么多字符，避免 settings.json 被长文撑大
DRAFT_MAX_CHARS = 20000


class _InputEdit(PlainTextEdit):
    """Editor that turns an image paste into an image translation."""

    imagePasted = pyqtSignal(QImage)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        is_paste = event.matches(QKeySequence.StandardKey.Paste) or (
            event.key() == Qt.Key.Key_V
            and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        )
        if is_paste:
            image = QGuiApplication.clipboard().image()
            if image is not None and not image.isNull():
                self.imagePasted.emit(image)
                return
        super().keyPressEvent(event)


def _hide_focus_layer(widget) -> None:
    """Fluent draws an accent underline on focus; our editors have no border."""
    for child in widget.findChildren(QWidget):
        if type(child).__name__ == "EditLayer":
            child.hide()


class TranslatePage(PageWidget):
    historyChanged = pyqtSignal()
    pinChanged = pyqtSignal(bool)

    def __init__(self, history_db: HistoryDB, glossary_db: GlossaryDB, parent=None) -> None:
        super().__init__(parent)
        self.history_db = history_db
        self.glossary_db = glossary_db
        self._worker: TranslationWorker | None = None
        self._image_path = ""
        self._image_url = ""
        self._image_qimage: QImage | None = None
        self._thumbnail: bytes | None = None
        self._started_at = 0.0
        self._notice = ""
        self._default_instruction = ""
        self._overlay = None
        self._last_clip_text = ""
        self._clip_timer = QTimer(self)
        self._clip_timer.setSingleShot(True)
        self._clip_timer.setInterval(280)  # 防抖：连续复制只取最后一次
        self._clip_timer.timeout.connect(self._apply_clipboard)
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)  # 防抖：停手 500ms 才写盘
        self._draft_timer.timeout.connect(self._save_draft)
        self._styles = load_styles(settings.get("styles", []))
        self._active_style = str(settings.get("active_style", "plain") or "")
        self._build_ui()
        self._restore_defaults()
        self._update_counter()
        self._set_status("就绪", colors()["accent"])
        on_theme_changed(self.apply_theme)
        self.restore_draft()
        self._connect_draft_signals()

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(12)

        outer.addWidget(self._build_language_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(12)
        splitter.setStyleSheet(
            "QSplitter::handle { background: transparent; }"
            "QSplitter::handle:hover { background: transparent; }"
        )
        splitter.addWidget(self._build_source_card())
        splitter.addWidget(self._build_result_card())
        splitter.setSizes([620, 620])
        outer.addWidget(splitter, 1)

        outer.addLayout(self._build_action_bar())
        self._retint_editors()
        QGuiApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

    def _build_language_bar(self) -> Card:
        card = Card(radius=10)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(10)

        self.src_combo = ComboBox()
        self.src_combo.setMinimumWidth(150)
        self.tgt_combo = ComboBox()
        self.tgt_combo.setMinimumWidth(150)
        self.swap_button = ToolButton(FluentIcon.SYNC)
        self.swap_button.setToolTip("交换源语言与目标语言")
        self.model_combo = ComboBox()
        self.model_combo.setMinimumWidth(150)
        self.model_combo.setToolTip("模型越大质量越好，越小速度越快")

        caption_src = MutedLabel("源语言", size=12)
        caption_tgt = MutedLabel("目标语言", size=12)
        caption_model = MutedLabel("模型", size=12)

        row.addWidget(caption_src)
        row.addWidget(self.src_combo)
        row.addWidget(self.swap_button)
        row.addSpacing(6)
        row.addWidget(caption_tgt)
        row.addWidget(self.tgt_combo)
        row.addStretch(1)
        row.addWidget(caption_model)
        row.addWidget(self.model_combo)
        layout.addLayout(row)

        self.instruction_edit = LineEdit()
        self.instruction_edit.setPlaceholderText(
            "可选指令：例如「保持简洁」「面向开发者」「术语按术语表」"
        )
        self.instruction_edit.setClearButtonEnabled(True)
        self.instruction_edit.setToolTip("作为附加约束发送给模型，留空则不附加")

        self.style_bar = StyleChipBar()
        self.style_bar.set_styles(self._styles, self._active_style)
        self.style_bar.stylePicked.connect(self._on_style_picked)
        self.style_bar.manageRequested.connect(self._manage_styles)
        layout.addWidget(self.style_bar)
        layout.addWidget(self.instruction_edit)

        self.instruction_edit.textChanged.connect(self._on_instruction_edited)
        return card

    # ------------------------------------------------------------- 风格预设
    def _on_style_picked(self, style_id: str) -> None:
        text = style_text(self._styles, style_id)
        self._active_style = style_id
        settings.set("active_style", style_id)
        settings.save()
        blocked = self.instruction_edit.blockSignals(True)
        self.instruction_edit.setText(text)
        self.instruction_edit.blockSignals(blocked)
        self._schedule_draft()

    def _on_instruction_edited(self, text: str) -> None:
        """手改文本时把芯片切到与之匹配的那个（没有就不高亮）。"""
        current = text.strip()
        matched = ""
        for style in self._styles:
            if style["text"].strip() == current:
                matched = style["id"]
                break
        if matched != self._active_style:
            self._active_style = matched
            self.style_bar.set_active(matched)

    def _manage_styles(self) -> None:
        dialog = StyleManagerDialog(self._styles, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._styles = dialog.styles()
        settings.set("styles", self._styles)
        settings.save()
        if find_style(self._styles, self._active_style) is None:
            self._active_style = self._styles[0]["id"] if self._styles else ""
        self.style_bar.set_styles(self._styles, self._active_style)

    def _build_source_card(self) -> Card:
        card = Card(radius=10)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(16, 12, 12, 10)
        header.setSpacing(8)
        title = StrongBodyLabel("原文")
        header.addWidget(title)
        header.addSpacing(4)
        self.src_hint = MutedLabel("Ctrl+Enter 翻译 · 可拖入图片", size=12)
        header.addWidget(self.src_hint)
        header.addStretch(1)

        self.image_button = ToolButton(FluentIcon.PHOTO)
        self.image_button.setToolTip("选择图片翻译（也可以直接拖入或粘贴截图）")
        self.shot_button = ToolButton(FluentIcon.CUT)
        self.shot_button.setToolTip("框选截屏翻译（Ctrl+Alt+S）")
        self.url_button = ToolButton(FluentIcon.LINK)
        self.url_button.setToolTip("使用图片链接（http/https）")
        self.remove_image_button = ToolButton(FluentIcon.DELETE)
        self.remove_image_button.setToolTip("移除图片，回到文本翻译")
        self.remove_image_button.setVisible(False)
        self.paste_button = ToolButton(FluentIcon.PASTE)
        self.paste_button.setToolTip("粘贴")
        self.clear_button = ToolButton(FluentIcon.BROOM)
        self.clear_button.setToolTip("清空")
        for button in (
            self.image_button,
            self.shot_button,
            self.url_button,
            self.remove_image_button,
            self.paste_button,
            self.clear_button,
        ):
            header.addWidget(button)
        layout.addLayout(header)
        layout.addWidget(Divider())

        self.stack = QStackedWidget()
        self.editor = _InputEdit()
        self.editor.setPlaceholderText("在此输入或粘贴要翻译的内容…")
        self.editor.setMinimumHeight(200)
        self.editor.document().setDocumentMargin(16)
        self.editor.setFrameShape(QFrame.Shape.NoFrame)

        self.drop_area = ImageDropArea()
        self.drop_area.setMinimumHeight(240)

        self.stack.addWidget(self.editor)
        self.stack.addWidget(self.drop_area)
        layout.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(16, 8, 12, 10)
        self.counter = MutedLabel("0 字", size=12)
        self.src_meta = MutedLabel("", size=12)
        footer.addWidget(self.counter)
        footer.addStretch(1)
        footer.addWidget(self.src_meta)
        layout.addLayout(footer)

        self.editor.textChanged.connect(self._update_counter)
        self.editor.imagePasted.connect(self.set_image_from_qimage)
        self.drop_area.fileDropped.connect(self.set_image_from_path)
        self.drop_area.clicked.connect(self.pick_image)
        self.image_button.clicked.connect(self.pick_image)
        self.shot_button.clicked.connect(self.start_screenshot)
        self.url_button.clicked.connect(self.pick_image_url)
        self.remove_image_button.clicked.connect(self.clear_image)
        self.paste_button.clicked.connect(self.paste_into_editor)
        self.clear_button.clicked.connect(self.clear_all)
        self.swap_button.clicked.connect(self.swap_languages)
        return card

    def _build_result_card(self) -> Card:
        card = Card(radius=10)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(16, 12, 12, 10)
        header.setSpacing(8)
        header.addWidget(StrongBodyLabel("译文"))
        self.result_hint = MutedLabel("", size=12)
        header.addWidget(self.result_hint)
        header.addStretch(1)
        self.copy_button = ToolButton(FluentIcon.COPY)
        self.copy_button.setToolTip("复制译文（Ctrl+Shift+C）")
        self.copy_button.setEnabled(False)
        header.addWidget(self.copy_button)
        layout.addLayout(header)
        layout.addWidget(Divider())

        self.output = TextBrowser()
        self.output.setPlaceholderText("译文会实时显示在这里")
        self.output.setOpenExternalLinks(False)
        self.output.document().setDocumentMargin(16)
        layout.addWidget(self.output, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(16, 8, 16, 10)
        footer.setSpacing(8)
        self.status_dot = StatusDot()
        self.status_label = MutedLabel("就绪", size=12)
        self.elapsed_label = MutedLabel("", size=12)
        footer.addWidget(self.status_dot)
        footer.addWidget(self.status_label)
        footer.addStretch(1)
        footer.addWidget(self.elapsed_label)
        layout.addLayout(footer)

        self.copy_button.clicked.connect(self.copy_result)
        return card

    def _build_action_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.translate_button = PrimaryPushButton("翻译")
        self.translate_button.setMinimumWidth(140)
        self.translate_button.setMinimumHeight(36)
        self.stop_button = PushButton("停止")
        self.stop_button.setMinimumWidth(96)
        self.stop_button.setMinimumHeight(36)
        self.stop_button.setEnabled(False)

        self.pin_button = TogglePushButton("窗口置顶")
        self.pin_button.setCheckable(True)
        self.pin_button.setProperty("role", "pin")
        self.pin_button.setMinimumHeight(36)
        self.pin_button.setToolTip("置顶后窗口不会被子文档、终端等盖住")
        self.watch_button = TogglePushButton("监听剪贴板")
        self.watch_button.setCheckable(True)
        self.watch_button.setProperty("role", "watch")
        self.watch_button.setMinimumHeight(36)
        self.watch_button.setToolTip("开启后，每次复制文字就自动翻译（窗口可见时立即开始）")

        hint = MutedLabel("免费公网服务，长文本可能需要几秒", size=12)
        row.addWidget(self.translate_button)
        row.addWidget(self.stop_button)
        row.addStretch(1)
        row.addWidget(hint)
        row.addSpacing(6)
        row.addWidget(self.pin_button)
        row.addWidget(self.watch_button)
        self.translate_button.clicked.connect(self.start_translation)
        self.stop_button.clicked.connect(self.stop_translation)
        self.pin_button.toggled.connect(self._on_pin_toggled)
        self.watch_button.toggled.connect(self._on_watch_toggled)
        return row

    def _retint_editors(self) -> None:
        c = colors()
        qss = EDITOR_QSS.format(text=c["text"], handle=c["border_strong"])
        self.editor.setStyleSheet(qss)
        self.output.setStyleSheet(qss)
        _hide_focus_layer(self.editor)
        _hide_focus_layer(self.output)

    # ------------------------------------------------------------------ state
    def _restore_defaults(self) -> None:
        from ..core.languages import target_languages

        self.src_combo.clear()
        self.src_combo.addItem("自动检测", userData="auto")
        for code, name in target_languages():
            self.src_combo.addItem(name, userData=code)

        self.tgt_combo.clear()
        for code, name in target_languages():
            self.tgt_combo.addItem(name, userData=code)

        self.model_combo.clear()
        for model_id, label in MODELS:
            self.model_combo.addItem(label, userData=model_id)

        self._select_lang(self.src_combo, settings.get("source_lang", "auto"))
        self._select_lang(self.tgt_combo, settings.get("target_lang", "zh"))
        self._select_lang(self.model_combo, settings.get("model", DEFAULT_MODEL), "35B")
        self._apply_default_instruction(settings.get("instruction", "") or "")

    @staticmethod
    def _select_lang(combo: ComboBox, value: str, fallback: str = "") -> None:
        for index in range(combo.count()):
            data = combo.itemData(index)
            if data == value or (fallback and data and fallback in str(data)):
                combo.setCurrentIndex(index)
                return
        if combo.count():
            combo.setCurrentIndex(0)

    def reload_settings(self) -> None:
        self._apply_default_instruction(settings.get("instruction", "") or "")
        self._select_lang(self.model_combo, settings.get("model", DEFAULT_MODEL), "35B")
        self._select_lang(self.tgt_combo, settings.get("target_lang", "zh"))
        self.sync_toggles()

    def sync_toggles(self) -> None:
        """把「置顶 / 监听剪贴板」按钮同步成设置里的状态（不触发副作用）。"""
        for button, key, default in (
            (self.pin_button, "always_on_top", False),
            (self.watch_button, "clipboard_watch", False),
        ):
            value = bool(settings.get(key, default))
            if button.isChecked() == value:
                self._refresh_toggle_text(button)
                continue
            button.blockSignals(True)
            button.setChecked(value)
            button.blockSignals(False)
            self._refresh_toggle_text(button)

    @staticmethod
    def _refresh_toggle_text(button) -> None:
        if button is None:
            return
        if button.property("role") == "pin":
            button.setText("已置顶" if button.isChecked() else "窗口置顶")
        else:
            button.setText("监听中" if button.isChecked() else "监听剪贴板")

    # ------------------------------------------------------- 置顶 / 剪贴板监听
    def _on_pin_toggled(self, checked: bool) -> None:
        self._refresh_toggle_text(self.pin_button)
        settings.set("always_on_top", bool(checked))
        settings.save()
        self.pinChanged.emit(bool(checked))

    def _on_watch_toggled(self, checked: bool) -> None:
        self._refresh_toggle_text(self.watch_button)
        settings.set("clipboard_watch", bool(checked))
        settings.save()
        if checked:
            self._last_clip_text = QGuiApplication.clipboard().text().strip()
            self._set_status("剪贴板监听已开启：复制文字即自动翻译", colors()["accent"])
        else:
            self._set_status("剪贴板监听已关闭", colors()["muted"])

    def _on_clipboard_changed(self) -> None:
        if self.watch_button.isChecked():
            self._clip_timer.start()

    def _apply_clipboard(self) -> None:
        """防抖后真正处理剪贴板内容：载入输入框，窗口可见时直接翻译。"""
        if not self.watch_button.isChecked():
            return
        text = QGuiApplication.clipboard().text().strip()
        if not text or text == self._last_clip_text:
            return
        self._last_clip_text = text
        if self.is_busy():
            return
        if len(text) > MAX_INPUT_CHARS:
            self._set_status(
                f"剪贴板内容 {len(text):,} 字，超过 {MAX_INPUT_CHARS:,} 字上限，未自动翻译",
                colors()["warn"],
            )
            return
        self.clear_image()
        self.editor.setPlainText(text)
        self._update_counter()
        window = self.window()
        visible = window is not None and window.isVisible() and not window.isMinimized()
        if visible:
            self._set_status("检测到剪贴板更新，正在翻译…", colors()["accent"])
            self.start_translation()
        else:
            self._set_status("剪贴板内容已载入，打开窗口后点「翻译」", colors()["muted"])

    def _apply_default_instruction(self, value: str) -> None:
        """Update the instruction box with the default, unless the user typed one."""
        current = self.instruction_edit.text().strip()
        if current and current != self._default_instruction:
            # The user wrote a one-off constraint; keep it.
            return
        self._default_instruction = value
        self.instruction_edit.setText(value)

    def apply_theme(self) -> None:
        self._retint_editors()

    def _set_status(self, text: str, color: str | None = None) -> None:
        self.status_label.setText(text)
        self.status_dot.set_color(color or colors()["accent"])

    def _update_counter(self) -> None:
        count = len(self.editor.toPlainText())
        self.counter.setText(f"{count:,} 字 · 上限 {MAX_INPUT_CHARS:,}")

    def _set_image_ui(self, has_image: bool) -> None:
        """The image caption carries the file details, so the text footer hides."""
        self.counter.setVisible(not has_image)
        self.src_meta.setText("")

    def _set_running(self, running: bool) -> None:
        self.translate_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def is_busy(self) -> bool:
        """True while a translation worker is alive."""
        return self._worker is not None

    # ------------------------------------------------------------------ 草稿
    def _connect_draft_signals(self) -> None:
        """草稿信号在恢复之后再接，避免启动时把默认值写回去。"""
        self.editor.textChanged.connect(self._schedule_draft)
        for combo in (self.src_combo, self.tgt_combo, self.model_combo):
            combo.currentIndexChanged.connect(lambda *_: self._schedule_draft())

    def _schedule_draft(self) -> None:
        if not bool(settings.get("draft_enabled", True)):
            return
        self._draft_timer.start()

    def _save_draft(self) -> None:
        if not bool(settings.get("draft_enabled", True)):
            return
        text = self.editor.toPlainText()
        if len(text) > DRAFT_MAX_CHARS:
            text = text[:DRAFT_MAX_CHARS]
        settings.set(
            "draft",
            {
                "text": text,
                "source_lang": self.src_combo.currentData() or "auto",
                "target_lang": self.tgt_combo.currentData() or "zh",
                "model": self.model_combo.currentData() or DEFAULT_MODEL,
            },
        )
        settings.save()

    def flush_draft(self) -> None:
        """退出前把还在防抖窗口里的草稿落盘。"""
        if self._draft_timer.isActive():
            self._draft_timer.stop()
            self._save_draft()

    def restore_draft(self) -> None:
        if not bool(settings.get("draft_enabled", True)):
            return
        draft = settings.get("draft", {})
        if not isinstance(draft, dict):
            return
        text = str(draft.get("text") or "")
        if text:
            self.editor.setPlainText(text[:DRAFT_MAX_CHARS])
            self._update_counter()
        pairs = (
            ("source_lang", self.src_combo, "auto"),
            ("target_lang", self.tgt_combo, "zh"),
            ("model", self.model_combo, DEFAULT_MODEL),
        )
        for key, combo, fallback in pairs:
            value = str(draft.get(key) or "") or fallback
            index = combo.findData(value)
            if index >= 0:
                blocked = combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(blocked)

    # ------------------------------------------------------------------ images
    def pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择图片", "", SUPPORTED_FILTER)
        if path:
            self.set_image_from_path(path)

    def start_screenshot(self) -> None:
        """框选截屏 → 直接进图片翻译队列。"""
        if self.is_busy():
            self._warn("正在翻译中", "请等这次翻译结束或点「停止」后再截屏。")
            return
        self.clear_image()
        self._set_status("请在屏幕上框选要翻译的区域…", colors()["accent"])
        overlay = CaptureOverlay()

        def on_captured(image: QImage) -> None:
            self._overlay = None
            self.set_image_from_qimage(image)
            self._set_status(f"已截取 {image.width()}×{image.height()}，正在开始翻译…", colors()["accent"])
            self.start_translation()

        def on_cancelled() -> None:
            self._overlay = None
            self._set_status("已取消截屏", colors()["muted"])

        overlay.captured.connect(on_captured)
        overlay.cancelled.connect(on_cancelled)
        self._overlay = overlay
        overlay.start()

    def set_image_from_path(self, path: str) -> None:
        if Path(path).suffix.lower() not in IMAGE_SUFFIXES:
            self._warn("无法识别该文件", "请选择 PNG / JPG / WEBP / BMP / GIF 图片。")
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._warn("图片无法读取", "文件可能已损坏，请换一张图片。")
            return
        self._image_path = path
        self._image_url = ""
        self._image_qimage = None
        self._thumbnail = None
        name = Path(path).name
        size = human_size(Path(path).stat().st_size)
        self.drop_area.set_image(pixmap, f"{name} · {pixmap.width()}×{pixmap.height()} · {size}")
        self.stack.setCurrentWidget(self.drop_area)
        self.remove_image_button.setVisible(True)
        self._set_image_ui(True)
        self._set_status("已载入图片，点击「翻译」识别并翻译", colors()["accent"])

    def pick_image_url(self) -> None:
        dialog = UrlInputDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        url = dialog.url()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            self._warn("链接无效", "请输入以 http:// 或 https:// 开头的图片链接。")
            return
        self._image_path = ""
        self._image_url = url
        self._image_qimage = None
        self._thumbnail = None
        name = url.split("?")[0].rsplit("/", 1)[-1] or "远程图片"
        self.drop_area.set_remote(name)
        self.stack.setCurrentWidget(self.drop_area)
        self.remove_image_button.setVisible(True)
        self._set_image_ui(True)
        self._set_status("已载入图片链接，点击「翻译」识别并翻译", colors()["accent"])

    def set_image_from_qimage(self, image: QImage) -> None:
        if image is None or image.isNull():
            return
        self._image_qimage = image.copy()
        self._image_path = ""
        self._image_url = ""
        self._thumbnail = None
        pixmap = QPixmap.fromImage(self._image_qimage)
        self.drop_area.set_image(pixmap, f"剪贴板图片 · {image.width()}×{image.height()}")
        self.stack.setCurrentWidget(self.drop_area)
        self.remove_image_button.setVisible(True)
        self._set_image_ui(True)
        self._set_status("已载入剪贴板图片，点击「翻译」识别并翻译", colors()["accent"])

    def clear_image(self) -> None:
        self._image_path = ""
        self._image_url = ""
        self._image_qimage = None
        self._thumbnail = None
        self.drop_area.clear()
        self.stack.setCurrentWidget(self.editor)
        self.remove_image_button.setVisible(False)
        self._set_image_ui(False)

    @property
    def has_image(self) -> bool:
        return bool(self._image_path) or bool(self._image_url) or self._image_qimage is not None

    # ------------------------------------------------------------------ actions
    def paste_into_editor(self) -> None:
        clipboard = QGuiApplication.clipboard()
        image = clipboard.image()
        if image is not None and not image.isNull():
            self.set_image_from_qimage(image)
            return
        text = clipboard.text()
        if text:
            self.editor.insertPlainText(text)
        else:
            self._warn("剪贴板为空", "没有可粘贴的文本或图片。")

    def clear_all(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.stop_translation()
        self.editor.clear()
        self.output.clear()
        self.clear_image()
        self.elapsed_label.setText("")
        self.result_hint.setText("")
        self.copy_button.setEnabled(False)
        self._set_status("就绪", colors()["accent"])

    def swap_languages(self) -> None:
        src = self.src_combo.currentData()
        tgt = self.tgt_combo.currentData()
        if not src or src == "auto" or not tgt:
            self._warn("无法交换", "源语言为「自动检测」时无法交换，请先选择具体语言。")
            return
        self._select_lang(self.src_combo, tgt)
        self._select_lang(self.tgt_combo, src)

    def copy_result(self) -> None:
        text = self.output.toPlainText().strip()
        if not text:
            return
        self._last_clip_text = text  # 避免自己被剪贴板监听再翻一遍
        QGuiApplication.clipboard().setText(text)
        InfoBar.success(
            title="已复制",
            content="译文已复制到剪贴板。",
            duration=1600,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    # -------------------------------------------------------------- translation
    def start_translation(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return

        text = self.editor.toPlainText().strip()
        if not self.has_image:
            if not text:
                self._warn("还没有内容", "请输入或粘贴要翻译的文本，或者拖入一张图片。")
                self.editor.setFocus()
                return
            if len(text) > MAX_INPUT_CHARS:
                self._warn(
                    "文本过长",
                    f"当前 {len(text):,} 字，超过单次上限 {MAX_INPUT_CHARS:,} 字，请分段翻译。",
                )
                return

        # 术语注入、缓存、限流、降级都由底座管道统一处理，这里不再预拼提示词。
        request = TranslationRequest(
            text=text,
            target_lang=self.tgt_combo.currentData() or "zh",
            source_lang=self.src_combo.currentData() or "auto",
            instruction=self.instruction_edit.text().strip(),
            model=self.model_combo.currentData() or settings.get("model", DEFAULT_MODEL),
            api_base=settings.get("api_base", ""),
            timeout=float(settings.get("timeout", 120) or 120),
            stream=bool(settings.get("stream", True)),
            temperature=float(settings.get("temperature", 0.0) or 0.0),
            image_path=self._image_path,
            image_url=self._image_url,
            image_qimage=self._image_qimage,
            kind="image" if self.has_image else "text",
        )

        self.output.clear()
        self.copy_button.setEnabled(False)
        self.elapsed_label.setText("")
        self._notice = ""
        self.result_hint.setText(_lang_label(self.tgt_combo))
        self._set_status("正在翻译…", colors()["accent"])
        self._set_running(True)
        self._started_at = time.monotonic()

        worker = TranslationWorker(request, self)
        worker.delta.connect(self._on_delta)
        worker.stage.connect(lambda message: self._set_status(message, colors()["accent"]))
        worker.notice.connect(self._on_notice)
        worker.first_token.connect(self._on_first_token)
        worker.thumbnail_ready.connect(self._on_thumbnail)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.stopped.connect(self._on_stopped)
        worker.finished.connect(self._on_thread_finished)
        self._worker = worker
        worker.start()

    def stop_translation(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._set_status("正在停止…", colors()["warn"])
            self._worker.cancel()

    def _at_bottom(self) -> bool:
        bar = self.output.verticalScrollBar()
        return bar.value() >= bar.maximum() - 8

    def _on_delta(self, chunk: str) -> None:
        stick = self._at_bottom()
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(chunk)
        if stick:
            bar = self.output.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _on_first_token(self) -> None:
        self._set_status("正在接收译文…", colors()["accent"])

    def _on_notice(self, text: str) -> None:
        """管道说点什么（缓存命中 / 已降级）——显示在状态行，最后并入耗时。"""
        self._notice = text
        self._set_status(text, colors()["accent"])

    def _on_thumbnail(self, thumbnail: object) -> None:
        if isinstance(thumbnail, (bytes, bytearray)):
            self._thumbnail = bytes(thumbnail)

    def _on_done(self, text: str, elapsed_ms: int) -> None:
        self._set_running(False)
        self._set_status("翻译完成", colors()["ok"])
        self.copy_button.setEnabled(bool(text.strip()))
        suffix = f" · {self._notice}" if self._notice else ""
        self.elapsed_label.setText(f"耗时 {elapsed_ms / 1000:.1f}s{suffix}")

        entry_id = self.history_db.add(
            kind="image" if self.has_image else "text",
            source_text=self.editor.toPlainText().strip(),
            result_text=text,
            source_lang=self.src_combo.currentData() or "auto",
            target_lang=self.tgt_combo.currentData() or "zh",
            model=self.model_combo.currentData() or "",
            image_thumb=self._thumbnail,
            elapsed_ms=elapsed_ms,
            keep=int(settings.get("keep_history", 500) or 0),
        )
        if entry_id:
            self.historyChanged.emit()

    def _on_failed(self, message: str, detail: str) -> None:
        self._set_running(False)
        self._set_status("翻译失败", colors()["danger"])
        bar = InfoBar.error(
            title="翻译失败",
            content=message,
            duration=6000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )
        if detail and bar is not None:
            bar.setToolTip(detail[:800])

    def _on_stopped(self, partial: str) -> None:
        self._set_running(False)
        self._set_status("已停止", colors()["warn"])
        if partial:
            self.copy_button.setEnabled(True)
            self.elapsed_label.setText(f"耗时 {time.monotonic() - self._started_at:.1f}s")

    def _on_thread_finished(self) -> None:
        self._set_running(False)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    def _warn(self, title: str, content: str) -> None:
        InfoBar.warning(
            title=title,
            content=content,
            duration=4000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    # ------------------------------------------------------------------ history
    def load_entry(self, entry) -> None:
        self.clear_image()
        if entry.source_text:
            self.editor.setPlainText(entry.source_text)
        else:
            self.editor.clear()
        self._select_lang(self.src_combo, entry.source_lang or "auto")
        self._select_lang(self.tgt_combo, entry.target_lang or "zh")
        self.output.setPlainText(entry.result_text or "")
        self.copy_button.setEnabled(bool((entry.result_text or "").strip()))
        self.result_hint.setText(_lang_label(self.tgt_combo))
        self.elapsed_label.setText(f"耗时 {entry.elapsed_ms / 1000:.1f}s" if entry.elapsed_ms else "")
        self._set_status("已载入历史记录", colors()["accent"])
        if entry.image_thumb:
            pixmap = QPixmap()
            if pixmap.loadFromData(entry.image_thumb):
                self.drop_area.set_image(pixmap, f"历史图片 · {entry.when}")
                self.stack.setCurrentWidget(self.drop_area)
                self.remove_image_button.setVisible(True)

    def load_text(self, text: str) -> None:
        """把外部文本（划词浮窗「转到主窗口」、剪贴板等）送进输入框。"""
        self.clear_image()
        self.editor.setPlainText(text or "")
        self._update_counter()
        self._set_status("就绪", colors()["muted"])
        self.focus_input()
        self.editor.moveCursor(QTextCursor.MoveOperation.Start)

    def focus_input(self) -> None:
        self.editor.setFocus()


def _lang_label(combo: ComboBox) -> str:
    return combo.currentText() or ""
