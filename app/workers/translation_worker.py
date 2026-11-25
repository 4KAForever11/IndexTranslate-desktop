"""Background translation worker.

The UI thread must never block on the network, so one worker per request is
started as a ``QThread``. Streaming deltas are batched (~50 ms) before being
emitted: a token-by-token signal storm makes the text view stutter.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from ..core.api_client import ApiError, ResponseHandle, translate
from ..core.image_utils import (
    fetch_remote_image,
    make_thumbnail,
    make_thumbnail_from_bytes,
    make_thumbnail_from_qimage,
    prepare_image,
    prepare_qimage,
)

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


class TranslationWorker(QThread):
    delta = pyqtSignal(str)
    first_token = pyqtSignal()
    stage = pyqtSignal(str)
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
            result = translate(
                text=request.text,
                image_data_url=image_data_url,
                target_lang=request.target_lang,
                source_lang=request.source_lang,
                instruction=request.instruction,
                glossary=request.glossary,
                model=request.model,
                api_base=request.api_base,
                timeout=request.timeout,
                stream=request.stream,
                temperature=request.temperature,
                cancel_event=self._cancel_event,
                handle=self._handle,
                on_delta=self._on_delta,
            )
            self._flush()

            if self._cancel_event.is_set():
                self.stopped.emit(result)
                return

            if not (result or "").strip():
                self.failed.emit("服务没有返回译文。", "empty result")
                return

            elapsed = int((time.monotonic() - started) * 1000)
            self.done.emit(result, elapsed)

        except ApiError as exc:
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
