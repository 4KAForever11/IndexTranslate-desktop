"""Background translation worker.

The UI thread must never block on the network, so one worker per request is
started as a ``QThread``. Streaming deltas are batched (~50 ms) before being
emitted: a token-by-token signal storm makes the text view stutter.

Since the V2 refactor the worker is a thin shell around
:func:`app.core.pipeline.get_pipeline` — the pipeline owns the glossary match,
the cache, the rate limiter and the 35B → 9B → 2B fallback. The worker only
prepares the image, forwards chunks, and reports what the pipeline decided
(缓存命中 / 已自动降级).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from ..core.config import DEFAULT_MODEL, model_label, settings
from ..core.http_client import ResponseHandle
from ..core.image_utils import (
    fetch_remote_image,
    make_thumbnail,
    make_thumbnail_from_bytes,
    make_thumbnail_from_qimage,
    prepare_image,
    prepare_qimage,
)
from ..core.models import ALL_GROUPS, EngineError, TranslateRequest as EngineRequest
from ..core.pipeline import get_pipeline

BATCH_INTERVAL = 0.05


@dataclass
class TranslationRequest:
    text: str = ""
    target_lang: str = "zh"
    source_lang: str = "auto"
    instruction: str = ""
    glossary: str = ""
    model: str = ""
    api_base: str = ""
    timeout: float = 120.0
    stream: bool = True
    temperature: float = 0.0
    image_path: str = ""
    image_url: str = ""
    image_qimage: object = None
    thumbnail: Optional[bytes] = None
    kind: str = "text"
    meta: dict = field(default_factory=dict)
    glossary_group: Optional[str] = None   # None = 用设置里的开关
    use_cache: bool = True
    scene: str = "translate"


def build_request(
    *,
    text: str = "",
    glossary: str = "",
    target_lang: str | None = None,
    source_lang: str | None = None,
    image_path: str = "",
    image_url: str = "",
    image_qimage: object = None,
    kind: str = "text",
    scene: str = "translate",
) -> TranslationRequest:
    """按当前设置组装请求，供浮窗 / 剪贴板监听等非主页入口复用。"""
    return TranslationRequest(
        text=text,
        target_lang=str(target_lang or settings.get("target_lang", "zh") or "zh"),
        source_lang=str(source_lang or settings.get("source_lang", "auto") or "auto"),
        instruction=str(settings.get("instruction", "") or ""),
        glossary=glossary,
        model=str(settings.get("model", "") or ""),
        api_base=str(settings.get("api_base", "") or ""),
        timeout=float(settings.get("timeout", 120) or 120),
        stream=bool(settings.get("stream", True)),
        temperature=float(settings.get("temperature", 0.0) or 0.0),
        image_path=image_path,
        image_url=image_url,
        image_qimage=image_qimage,
        kind=kind,
        scene=scene,
    )


class TranslationWorker(QThread):
    delta = pyqtSignal(str)
    first_token = pyqtSignal()
    stage = pyqtSignal(str)
    notice = pyqtSignal(str)          # 缓存命中 / 已自动降级至 9B
    thumbnail_ready = pyqtSignal(object)
    done = pyqtSignal(str, int)
    failed = pyqtSignal(str, str)
    stopped = pyqtSignal(str)

    def __init__(self, request: TranslationRequest, parent=None) -> None:
        super().__init__(parent)
        self.request = request
        self._cancel_event = threading.Event()
        self._handle = ResponseHandle()
        self._pending: list[str] = []
        self._emitted_any = False
        self._last_flush = 0.0
        self._notices: set[str] = set()

    # ------------------------------------------------------------------ public
    def cancel(self) -> None:
        self._cancel_event.set()
        self._handle.cancel()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ------------------------------------------------------------------- hooks
    def _on_delta(self, piece: str) -> None:
        if not piece:
            return
        self._pending.append(piece)
        now = time.monotonic()
        if now - self._last_flush >= BATCH_INTERVAL:
            self._flush()
        if not self._emitted_any:
            self._emitted_any = True
            self.first_token.emit()

    def _flush(self) -> None:
        if not self._pending:
            return
        chunk = "".join(self._pending)
        self._pending.clear()
        self._last_flush = time.monotonic()
        if chunk:
            self.delta.emit(chunk)

    def _note(self, text: str) -> None:
        if text and text not in self._notices:
            self._notices.add(text)
            self.notice.emit(text)

    # --------------------------------------------------------------------- run
    def run(self) -> None:  # noqa: C901 - linear, readable flow
        request = self.request
        started = time.monotonic()
        try:
            image_data_url = ""
            thumbnail = request.thumbnail

            if request.image_qimage is not None:
                self.stage.emit("正在压缩图片…")
                payload = prepare_qimage(request.image_qimage)
                image_data_url = payload.data_url
                if thumbnail is None:
                    thumbnail = make_thumbnail_from_qimage(request.image_qimage)
            elif request.image_url:
                self.stage.emit("正在下载图片…")
                payload = fetch_remote_image(request.image_url)
                image_data_url = payload.data_url
                if thumbnail is None:
                    thumbnail = make_thumbnail_from_bytes(payload.data_url)
            elif request.image_path:
                self.stage.emit("正在处理图片…")
                payload = prepare_image(request.image_path)
                image_data_url = payload.data_url
                if thumbnail is None and not request.image_path.startswith(("http", "data:")):
                    thumbnail = make_thumbnail(request.image_path)

            if self._cancel_event.is_set():
                self.stopped.emit("")
                return

            if thumbnail:
                self.thumbnail_ready.emit(thumbnail)

            self.stage.emit("正在翻译…")
            model = request.model or str(settings.get("model", DEFAULT_MODEL))
            use_glossary = bool(settings.get("use_glossary", True))
            if request.glossary_group is not None:
                group: Optional[str] = request.glossary_group
            else:
                group = ALL_GROUPS if use_glossary else None

            engine_request = EngineRequest(
                text=request.text,
                image_b64=(image_data_url or None),
                source_lang=request.source_lang or "auto",
                target_lang=request.target_lang or "zh",
                instruction=request.instruction or "",
                model=model,
                glossary_group=group,
                use_cache=bool(request.use_cache),
                stream=bool(request.stream),
                scene=request.scene or "translate",
                extra={
                    "api_base": request.api_base,
                    "timeout": float(request.timeout or 120.0),
                    "temperature": float(request.temperature or 0.0),
                    "handle": self._handle,
                    "use_glossary": use_glossary,
                },
            )

            pipeline = get_pipeline()
            text_parts: list[str] = []
            for chunk in pipeline.execute_stream(engine_request, self._cancel_event):
                if chunk.from_cache:
                    self._note("缓存命中")
                if chunk.model_used and chunk.model_used != model:
                    self._note(f"已自动降级至 {model_label(chunk.model_used)}")
                if chunk.delta:
                    text_parts.append(chunk.delta)
                    self._on_delta(chunk.delta)

            self._flush()
            result = "".join(text_parts)

            if self._cancel_event.is_set():
                self.stopped.emit(result)
                return

            if not result.strip():
                self.failed.emit("服务没有返回译文。", "empty result")
                return

            elapsed = int((time.monotonic() - started) * 1000)
            self.done.emit(result, elapsed)

        except EngineError as exc:
            self._flush()
            if self._cancel_event.is_set():
                self.stopped.emit("".join(self._pending))
                return
            self.failed.emit(exc.message, exc.detail)
        except ValueError as exc:
            self.failed.emit(str(exc), "")
        except Exception as exc:  # unexpected: never let the UI hang
            self._flush()
            if self._cancel_event.is_set():
                self.stopped.emit("")
                return
            self.failed.emit("翻译失败，请稍后重试。", repr(exc))
