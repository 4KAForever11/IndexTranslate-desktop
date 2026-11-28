"""实时字幕条：置顶、半透明、可拖动，显示「原文 / 译文」两行。

数据来自 ``ext-live-asr`` 扩展（系统回环音频 → VAD 断句 → 本地语音识别），
翻译仍然交给宿主的 :class:`~app.core.pipeline.TranslationPipeline`——
这样术语表、缓存、限流、降级全都自动生效，扩展本身不碰网络。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QByteArray, QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath
from PyQt6.QtWidgets import QWidget
from qfluentwidgets import FluentIcon, TransparentToolButton

from ..core import asr_models, settings
from ..core.extensions.runtime import ExtensionRuntime
from ..core.extensions.sidecar_client import SidecarError
from ..core.languages import display_name
from ..core.models import ALL_GROUPS, EngineError, TranslateRequest
from ..core.pipeline import get_pipeline
from .theme import ACCENT

ASR_ID = "ext-live-asr"
BAR_WIDTH = 900
BAR_HEIGHT = 108
MAX_LINES = 3
CONTEXT_SENTENCES = 2
# 连续语音里扩展可能很久不给 final（VAD 等不到静音），这时用 partial 顶上：
# 停手 700 ms 之后的最新 partial 当作临时句子去翻，final 来了再覆盖。
PARTIAL_DEBOUNCE_MS = 700
PARTIAL_MIN_AGE_MS = 1200


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

        # 有些窗口管理器会把 Tool 窗口压到别的窗口下面（尤其边框全屏的视频播放器），
        # 定时把自己再抬一次，比只 show() 一次可靠得多。
        self._keep_on_top = QTimer(self)
        self._keep_on_top.setInterval(2500)
        self._keep_on_top.timeout.connect(self._tick_topmost)

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
        area = screen.availableGeometry() if screen else None
        if area is None:
            return
        width = min(BAR_WIDTH, max(320, area.width() - 40))
        self.setFixedSize(width, BAR_HEIGHT)
        self.move(
            area.center().x() - width // 2,
            area.bottom() - BAR_HEIGHT - 72,
        )

    # ------------------------------------------------------------ 可见性自检
    def apply_saved_geometry(self, encoded: str) -> None:
        """恢复上次的位置；越界（换过显示器/分辨率）就回到默认位置。"""
        restored = False
        if encoded:
            try:
                restored = bool(
                    self.restoreGeometry(QByteArray.fromBase64(encoded.encode("ascii")))
                )
            except Exception:  # noqa: BLE001 - 坏数据不该挡住显示
                restored = False
        if not restored or not self._on_some_screen(self.frameGeometry()):
            self.move_to_default()

    @staticmethod
    def _on_some_screen(rect) -> bool:
        for screen in QGuiApplication.screens():
            hit = screen.availableGeometry().intersected(rect)
            # 至少要露出一块能看清/能拖的区域，否则等于没显示
            if hit.width() >= 160 and hit.height() >= 60:
                return True
        return False

    def ensure_visible(self) -> None:
        """把字幕条摆到能看见的地方，并在顶层重新亮一次。"""
        if not self._on_some_screen(self.frameGeometry()):
            self.move_to_default()
        if not self.isVisible():
            self.show()
        self.raise_()

    def _tick_topmost(self) -> None:
        if not self.isVisible():
            self._keep_on_top.stop()
            return
        self.raise_()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._keep_on_top.isActive():
            self._keep_on_top.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._keep_on_top.stop()
        super().hideEvent(event)

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
    # 扩展的回调在 sidecar 读线程里，所有计时器与翻译都要回到主线程再动
    _wake = pyqtSignal()
    _finalText = pyqtSignal(str)

    def __init__(self, runtime: ExtensionRuntime, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._client = None
        self._history: deque[str] = deque(maxlen=CONTEXT_SENTENCES)
        self._cancel = threading.Event()
        self._busy = False
        self._lock = threading.Lock()
        self._device_name = ""
        self._model = str(settings.get("replace_model") or "Index-Translate-2B")
        self._last_translated = ""
        self._pending_source = ""
        self._partial_at = 0.0
        # 部分结果防抖：既不能每 0.9 秒发一次请求把限流打满，也不能一直等 final
        self._partial_timer = QTimer(self)
        self._partial_timer.setSingleShot(True)
        self._partial_timer.timeout.connect(self._flush_pending)
        self._partial_timer.setInterval(PARTIAL_DEBOUNCE_MS)
        self._wake.connect(self._on_wake)
        self._finalText.connect(self._translate)
        self.sentenceReady.connect(self._on_sentence_ready)

    # ---------------------------------------------------------------- 语言
    def source_language(self) -> str:
        """识别与翻译用的源语言：扩展自己的设置优先，回落到主设置。"""
        value = str(settings.get("live_asr_source_lang") or "").strip()
        return value or str(settings.get("source_lang") or "auto")

    def target_language(self) -> str:
        value = str(settings.get("live_asr_target_lang") or "").strip()
        return value or str(settings.get("target_lang") or "zh")

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
        model_dir = self._resolve_model()
        if not model_dir:
            self.failed.emit(
                "识别模型还没下载。请到「扩展中心 → 实时字幕设置」点「下载模型」"
                f"（{asr_models.total_bytes() / 1048576:.0f} MB，下载一次即可离线使用）。"
            )
            return False
        device = str(settings.get("live_asr_device", "") or "")
        params = {
            "model": model_dir,
            # 送给识别器的是「源语言」，不是目标语言；auto 用空串让模型自己判
            "language": _asr_language(self.source_language()),
            "threshold": 0.5,
            "silence_sec": 0.55,
            "min_speech_sec": 0.35,
        }
        if device:
            params["device"] = int(device) if device.isdigit() else device
        try:
            client.add_subscriber(self._on_event)
            client.call("start", params, timeout=30.0)
        except Exception as exc:
            client.remove_subscriber(self._on_event)
            self.failed.emit(_friendly(exc))
            return False
        self._client = client
        self._cancel.clear()
        self._device_name = ""
        self._last_translated = ""
        self._pending_source = ""
        self.statusChanged.emit(
            f"正在监听系统音频…（{_lang_pair(self.source_language(), self.target_language())}）"
        )
        return True

    def _resolve_model(self) -> str:
        """优先用设置里记录的目录；没有就把已下载好的默认目录接上。"""
        current = str(self._runtime.model_path(ASR_ID) or "")
        if current and asr_models.is_ready(current):
            return current
        ready = asr_models.configured_dir()
        if ready:
            try:
                asr_models.ensure_configured(Path(ready))
            except asr_models.AsrModelError:
                return ""
            return ready
        return ""

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
                # final 来了就以它为准，丢掉还没翻的临时句子
                self._partial_timer.stop()
                self._pending_source = ""
                self._finalText.emit(text)
        elif event == "partial":
            text = str(data.get("text") or "").strip()
            if text:
                # 连续讲话时扩展可能长时间不出 final：先拿最新 partial 顶上
                self._pending_source = text
                self._partial_at = time.monotonic()
                self._wake.emit()
                self.statusChanged.emit(f"识别中… {text[:40]}")
        elif event == "ready":
            # 扩展真正把回环流打开之后才报 ready：在这之前说「监听中」是假的
            device = str(data.get("device") or "")
            if device:
                self._device_name = device
            rate = int(data.get("rate") or 0)
            suffix = f" · {rate // 1000} kHz" if rate else ""
            self.statusChanged.emit(
                f"监听中 · {self._device_name or '系统音频'}{suffix} · 等待声音…"
            )
        elif event == "level":
            # 扩展每 0.5 秒报一次电平：抓不到声音时这里会一直是空的
            rms = float(data.get("rms") or 0.0)
            device = str(data.get("device") or "")
            if device:
                self._device_name = device
            self.statusChanged.emit(
                f"监听中 · {self._device_name or '系统音频'} · 电平 {_level_bar(rms)}"
            )
        elif event == "warning":
            text = str(data.get("message") or "").strip()
            if text:
                self.statusChanged.emit(text)
        elif event == "error":
            self.failed.emit(str(data.get("message") or "识别扩展报错。"))

    def _on_wake(self) -> None:
        """自动防抖：每条新 partial 都把计时器往后推一点。"""
        if self._client is None or self._cancel.is_set():
            return
        if not self._busy:
            self._partial_timer.start()

    def _flush_pending(self) -> None:
        """把攒下来的最新 partial 送去翻译（防抖后的出口）。"""
        if self._client is None or self._cancel.is_set():
            return
        text = self._pending_source.strip()
        if not text:
            return
        if self._busy:
            # 上一句还没回来：留着，等它结束再翻最新的一句
            self._partial_timer.start()
            return
        age_ms = (time.monotonic() - self._partial_at) * 1000.0
        if age_ms < PARTIAL_MIN_AGE_MS:
            self._partial_timer.start(int(PARTIAL_MIN_AGE_MS - age_ms) + 50)
            return
        self._pending_source = ""
        self._translate(text)

    def _translate(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._busy:
            # 上一句还在翻：记下最新的，翻完再补，别把话直接扔掉
            self._pending_source = text
            return
        if text == self._last_translated:
            return
        self._busy = True
        self._last_translated = text
        context = "".join(self._history)
        instruction = (
            f"这些是视频里连续出现的台词，上一句是「{context}」，请保持人称与语气一致。"
            if context
            else ""
        )
        request = TranslateRequest(
            text=text,
            source_lang=self.source_language(),
            target_lang=self.target_language(),
            instruction=instruction,
            model=self._model,
            glossary_group=ALL_GROUPS if settings.get("use_glossary", True) else None,
            use_cache=True,
            stream=False,
            scene="polish",
        )

        def worker() -> None:
            translated = ""
            try:
                result = ""
                for chunk in get_pipeline().execute_stream(request, self._cancel):
                    if chunk.delta:
                        result += chunk.delta
                translated = result.strip()
            except EngineError as exc:
                # 单句翻译失败不能把整场字幕关掉：状态栏报一句，下一句继续试
                self.statusChanged.emit(f"这一句没翻出来：{exc.message[:80]}")
            except Exception as exc:  # noqa: BLE001 - 网络以外的意外也只报一句
                self.statusChanged.emit(f"这一句没翻出来：{_friendly(exc)[:80]}")
            finally:
                with self._lock:
                    self._busy = False
            if translated:
                self._history.append(text)
                self.sentenceReady.emit(text, translated)
            if self._pending_source:
                # 这一句翻完的期间又攒了新句子：回到主线程接着翻
                self._wake.emit()

        threading.Thread(target=worker, name="live-subtitle-translate", daemon=True).start()

    def _on_sentence_ready(self, source: str, translated: str) -> None:
        self.statusChanged.emit("已翻译")


def _level_bar(rms: float) -> str:
    """把 RMS 音量画成 6 格电平条，方便一眼看出「根本没收到声音」。"""
    level = max(0.0, min(1.0, rms * 12.0))
    filled = int(round(level * 6))
    return "▮" * filled + "▯" * (6 - filled)


def _asr_language(source_lang: str) -> str:
    """把宿主的语言代码转成 sherpa-onnx 认识的语言名。

    ``auto`` 交给模型自己判断（SenseVoice 自带语种识别），
    其余取主标签：``en`` / ``zh`` / ``ja`` / ``ko`` / ``yue``。
    """
    if not source_lang or source_lang in ("auto", "自动检测"):
        return ""
    return source_lang.split("_")[0]


def _lang_pair(source_lang: str, target_lang: str) -> str:
    """状态栏里的「源 → 目标」，让人一眼确认语言配对没搞反。"""
    left = "自动" if not source_lang or source_lang in ("auto", "自动检测") else display_name(source_lang)
    return f"{left} → {display_name(target_lang)}"


def _friendly(exc: Exception) -> str:
    if isinstance(exc, SidecarError):
        return str(exc)
    return f"实时字幕出错：{exc}"