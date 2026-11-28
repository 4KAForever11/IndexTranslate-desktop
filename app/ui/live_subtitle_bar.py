"""实时字幕条：置顶、半透明、可拖动，显示「原文 / 译文」两行。

数据来自 ``ext-live-asr`` 扩展（系统回环音频 → VAD 断句 → 本地语音识别），
翻译仍然交给宿主的 :class:`~app.core.pipeline.TranslationPipeline`——
这样术语表、缓存、限流、降级全都自动生效，扩展本身不碰网络。
"""
from __future__ import annotations

import threading
from collections import deque

from PyQt6.QtCore import QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath
from PyQt6.QtWidgets import QWidget
from qfluentwidgets import FluentIcon, TransparentToolButton

from ..core import settings
from ..core.extensions.runtime import ExtensionRuntime
from ..core.extensions.sidecar_client import SidecarError
from ..core.models import ALL_GROUPS, EngineError, TranslateRequest
from ..core.pipeline import get_pipeline
from .theme import ACCENT

ASR_ID = "ext-live-asr"
BAR_WIDTH = 900
BAR_HEIGHT = 108
MAX_LINES = 3
CONTEXT_SENTENCES = 2


class SubtitleBar(QWidget):
    """置顶悬浮字幕条（无边框、半透明、可拖动）。"""

    closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(BAR_WIDTH, BAR_HEIGHT)
        self._source = ""
        self._translated = ""
        self._status = "等待字幕…"
        self._drag_offset: QPoint | None = None

        self._close_button = TransparentToolButton(FluentIcon.CLOSE, self)
        self._close_button.setFixedSize(28, 28)
        self._close_button.move(BAR_WIDTH - 38, 8)
        self._close_button.clicked.connect(self.close_bar)

    # ---------------------------------------------------------------- 内容
    def set_text(self, source: str, translated: str, *, status: str = "") -> None:
        self._source = (source or "").strip()
        self._translated = (translated or "").strip()
        if status:
            self._status = status
        self.update()

    def set_status(self, text: str) -> None:
        self._status = text
        self.update()

    def clear_text(self) -> None:
        self._source = ""
        self._translated = ""
        self.update()

    def close_bar(self) -> None:
        self.hide()
        self.closed.emit()

    # ---------------------------------------------------------------- 拖动
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None

    def move_to_default(self) -> None:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(
            area.center().x() - BAR_WIDTH // 2,
            area.bottom() - BAR_HEIGHT - 72,
        )

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width() - 1, self.height() - 1, 14, 14)
        painter.fillPath(path, QColor(18, 18, 20, 205))
        painter.setPen(QColor(255, 255, 255, 26))
        painter.drawPath(path)

        painter.setPen(QColor(150, 150, 158))
        small = QFont(self.font())
        small.setPixelSize(11)
        painter.setFont(small)
        painter.drawText(18, 20, self._status)

        source = _wrap(self._source, 46, MAX_LINES)
        translated = _wrap(self._translated, 42, MAX_LINES)

        font = QFont(self.font())
        font.setPixelSize(14)
        painter.setFont(font)
        painter.setPen(QColor(170, 172, 180))
        painter.drawText(18, 42, self.width() - 60, 24, int(Qt.AlignmentFlag.AlignLeft), source)

        font.setPixelSize(19)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(245, 246, 250))
        painter.drawText(
            18,
            68,
            self.width() - 60,
            34,
            int(Qt.AlignmentFlag.AlignLeft),
            translated or "（等待识别…）",
        )
        painter.setPen(QColor(ACCENT))


def _wrap(text: str, width_chars: int, max_lines: int) -> str:
    if not text:
        return ""
    lines: list[str] = []
    current = ""
    for char in text:
        current += char
        if len(current) >= width_chars:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        lines[0] = "…" + lines[0][1:]
    return "\n".join(line.strip() for line in lines if line.strip())


class LiveSubtitleController(QObject):
    """把扩展推来的句子翻成译文并驱动字幕条。"""

    sentenceReady = pyqtSignal(str, str)
    statusChanged = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, runtime: ExtensionRuntime, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._client = None
        self._history: deque[str] = deque(maxlen=CONTEXT_SENTENCES)
        self._cancel = threading.Event()
        self._busy = False
        self._lock = threading.Lock()
        self._model = str(settings.get("replace_model") or "Index-Translate-2B")
        self.sentenceReady.connect(self._on_sentence_ready)

    # ---------------------------------------------------------------- 状态
    @property
    def running(self) -> bool:
        return self._client is not None

    def _client_or_none(self):
        client = self._runtime.client(ASR_ID)
        if client is None:
            self.failed.emit(
                "实时字幕扩展未安装或未启用：请到「扩展中心」安装 ext-live-asr 并启用。"
            )
            return None
        return client

    # ---------------------------------------------------------------- 开关
    def start(self) -> bool:
        if self.running:
            return True
        client = self._client_or_none()
        if client is None:
            return False
        model_dir = str(self._runtime.model_path(ASR_ID) or "")
        if not model_dir:
            self.failed.emit(
                "还没有选择语音识别模型目录（扩展中心 → 实时字幕 → 选择模型目录）。"
            )
            return False
        try:
            client.add_subscriber(self._on_event)
            client.call(
                "start",
                {
                    "model": model_dir,
                    "language": str(settings.get("target_lang") or "zh"),
                    "threshold": 0.5,
                    "silence_sec": 0.55,
                    "min_speech_sec": 0.35,
                },
                timeout=30.0,
            )
        except Exception as exc:
            client.remove_subscriber(self._on_event)
            self.failed.emit(_friendly(exc))
            return False
        self._client = client
        self._cancel.clear()
        self.statusChanged.emit("正在监听系统音频…")
        return True

    def stop(self) -> None:
        client = self._client
        self._client = None
        self._cancel.set()
        if client is None:
            return
        try:
            client.remove_subscriber(self._on_event)
            client.call("stop", {}, timeout=10.0)
        except Exception:  # 退出路径不打扰用户
            pass
        self.statusChanged.emit("已停止监听")

    # ---------------------------------------------------------------- 事件
    def _on_event(self, message: dict) -> None:
        if self._client is None:
            return
        event = str(message.get("event") or "")
        data = message.get("data") or {}
        if event == "final":
            text = str(data.get("text") or "").strip()
            if text:
                self._translate(text)
        elif event == "partial":
            text = str(data.get("text") or "").strip()
            if text:
                self.statusChanged.emit(f"识别中… {text[:40]}")
        elif event == "error":
            self.failed.emit(str(data.get("message") or "识别扩展报错。"))

    def _translate(self, text: str) -> None:
        if self._busy:
            # 上一句还在翻：直接跳过，字幕宁可漏一句，也不要堆积延迟
            return
        self._busy = True
        context = "".join(self._history)
        instruction = (
            f"这些是视频里连续出现的台词，上一句是「{context}」，请保持人称与语气一致。"
            if context
            else ""
        )
        request = TranslateRequest(
            text=text,
            source_lang=str(settings.get("source_lang") or "auto"),
            target_lang=str(settings.get("target_lang") or "zh"),
            instruction=instruction,
            model=self._model,
            glossary_group=ALL_GROUPS if settings.get("use_glossary", True) else None,
            use_cache=True,
            stream=False,
            scene="polish",
        )

        def worker() -> None:
            try:
                result = ""
                for chunk in get_pipeline().execute_stream(request, self._cancel):
                    if chunk.delta:
                        result += chunk.delta
                translated = result.strip()
            except EngineError as exc:
                self.failed.emit(exc.message)
                translated = ""
            except Exception as exc:  # 网络以外的意外，也只报一句
                self.failed.emit(_friendly(exc))
                translated = ""
            finally:
                with self._lock:
                    self._busy = False
            if translated:
                self._history.append(text)
                self.sentenceReady.emit(text, translated)

        threading.Thread(target=worker, name="live-subtitle-translate", daemon=True).start()

    def _on_sentence_ready(self, source: str, translated: str) -> None:
        self.statusChanged.emit("已翻译")


def _friendly(exc: Exception) -> str:
    if isinstance(exc, SidecarError):
        return str(exc)
    return f"实时字幕出错：{exc}"