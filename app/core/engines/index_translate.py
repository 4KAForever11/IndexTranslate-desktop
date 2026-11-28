"""Index-Translate engine: the free public Bilibili endpoint.

This is the code that used to live in :mod:`app.core.api_client`, moved behind
the :class:`~app.core.engines.base.BaseEngine` interface without changing a
single byte of the prompts or of the SSE handling — the prompt parity tests
compare against ``call_api.py`` and must stay green.

Behaviour worth knowing:

* ``chat_template_kwargs.enable_thinking`` is always ``False``. With thinking
  on, the model's chain-of-thought can replace the translation entirely.
* A transient failure (429 / 5xx / timeout) is retried **once** for this model,
  but only while nothing has been emitted. If tokens already reached the user,
  the error propagates: silently restarting would duplicate text.
* Cancellation is checked per SSE line, and yields whatever was already
  produced — 停止 keeps the partial translation instead of discarding it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterator

from ..config import API_BASE
from ..http_client import (
    HEADERS,
    ResponseHandle,
    build_chat_payload,
    error_for_status,
    iter_sse_text,
    new_client,
    read_json_text,
    transport_error,
)
from ..models import (
    EngineCapabilities,
    EngineError,
    RetryableEngineError,
    TranslateChunk,
    TranslateRequest,
)
from ..prompt_builder import build_image_prompt, build_prompt, strip_think
from .base import BaseEngine

#: Backoff before the single same-model retry.
RETRY_DELAY = 1.2


class IndexTranslateEngine(BaseEngine):
    """Streaming client for the public Index-Translate service."""

    engine_id = "index_translate"
    display_name = "Index-Translate"
    capabilities = EngineCapabilities(supports_stream=True, supports_image=True)

    def __init__(self, base_url: str = API_BASE, timeout: float = 45.0) -> None:
        self.base_url = (base_url or API_BASE).rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------- overridable
    def _base_url(self, req: TranslateRequest) -> str:
        return str(req.extra.get("api_base") or self.base_url or API_BASE).rstrip("/")

    def _headers(self, req: TranslateRequest) -> dict[str, str]:
        return dict(HEADERS)

    def _payload(
        self,
        req: TranslateRequest,
        content: str | list[dict[str, Any]],
        *,
        stream: bool,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        return build_chat_payload(
            req.model,
            content,
            stream=stream,
            max_tokens=max_tokens,
            temperature=temperature,
            native=True,
        )

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _data_url(image_b64: str) -> str:
        value = (image_b64 or "").strip()
        if value.startswith("data:"):
            return value
        return f"data:image/png;base64,{value}"

    def _content(
        self, req: TranslateRequest, matched_terms: list[str]
    ) -> tuple[str | list[dict[str, Any]], int]:
        glossary = "、".join(matched_terms)
        if req.is_image:
            return (
                build_image_prompt(
                    self._data_url(req.image_b64 or ""),
                    req.target_lang,
                    req.source_lang,
                    ref_text=req.text,
                    instruction=req.instruction,
                    glossary=glossary,
                ),
                2048,
            )
        content = build_prompt(
            req.text,
            req.target_lang,
            req.source_lang,
            instruction=req.instruction,
            glossary=glossary,
        )
        # Generous but bounded: the endpoint truncates anything above its cap.
        max_tokens = min(4096, max(1024, int(len(req.text) * 1.25) + 256))
        return content, max_tokens

    # ------------------------------------------------------------------- main
    def translate_stream(
        self,
        req: TranslateRequest,
        matched_terms: list[str],
        cancel_event: threading.Event,
    ) -> Iterator[TranslateChunk]:
        cancel = cancel_event or threading.Event()
        if cancel.is_set():
            return

        content, max_tokens = self._content(req, matched_terms)
        temperature = float(req.extra.get("temperature") or 0.0)
        timeout = float(req.extra.get("timeout") or self.timeout)
        handle: ResponseHandle | None = req.extra.get("handle")
        payload = self._payload(
            req,
            content,
            stream=bool(req.stream),
            max_tokens=max_tokens,
            temperature=temperature,
        )
        url = f"{self._base_url(req)}/chat/completions"
        headers = self._headers(req)

        emitted = ""
        last_error: EngineError | None = None

        for attempt in (0, 1):
            try:
                with new_client(timeout) as client:
                    if req.stream:
                        with client.stream("POST", url, json=payload, headers=headers) as response:
                            if handle is not None and handle.bind(response):
                                return
                            if response.status_code >= 400:
                                body = response.read().decode("utf-8", "replace")
                                raise error_for_status(response.status_code, body)

                            content_type = response.headers.get("content-type", "")
                            if "application/json" in content_type and "event-stream" not in content_type:
                                # The server ignored stream=true and answered in one body.
                                text = strip_think(read_json_text(response))
                                if not text:
                                    raise RetryableEngineError("服务没有返回译文，请稍后重试。")
                                yield self._chunk(text, req, done=True)
                                return

                            for piece in iter_sse_text(response.iter_lines(), cancel):
                                emitted += piece
                                yield self._chunk(piece, req)

                        if cancel.is_set():
                            return
                        if not emitted.strip():
                            raise RetryableEngineError("服务没有返回译文，请稍后重试。")
                        yield self._chunk("", req, done=True)
                        return

                    response = client.post(url, json=payload, headers=headers)
                    if response.status_code >= 400:
                        raise error_for_status(response.status_code, response.text)
                    text = strip_think(read_json_text(response))
                    if not text:
                        raise RetryableEngineError("服务没有返回译文，请稍后重试。")
                    yield self._chunk(text, req, done=True)
                    return

            except EngineError as exc:
                last_error = exc
                if cancel.is_set():
                    return
                if emitted or not isinstance(exc, RetryableEngineError) or attempt == 1:
                    raise
            except Exception as exc:  # httpx transport errors and anything unexpected
                if cancel.is_set():
                    return
                last_error = transport_error(exc)
                if emitted or attempt == 1:
                    raise last_error from exc

            if cancel.wait(RETRY_DELAY):
                return

        raise last_error or EngineError("翻译请求失败，请稍后重试。")

    # ------------------------------------------------------------------ chunks
    def _chunk(self, piece: str, req: TranslateRequest, *, done: bool = False) -> TranslateChunk:
        return TranslateChunk(
            delta=piece,
            done=done,
            engine_used=self.engine_id,
            model_used=req.model,
        )
