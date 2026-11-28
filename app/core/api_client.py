"""Compatibility facade over the V2 engine layer.

The translation core moved to :mod:`app.core.pipeline` + :mod:`app.core.engines`.
This module stays behind so that code written before the refactor keeps working
unchanged: the worker still imports ``ApiError``/``ResponseHandle``/``translate``,
``image_utils`` and ``proxy_server`` still import ``new_client``, and the
prompt-parity tests still call ``build_payload``.

New code should talk to the pipeline directly; everything here is a thin
delegation with no logic of its own beyond argument plumbing.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterator

from .config import API_BASE, DEFAULT_MODEL
from .engines.index_translate import IndexTranslateEngine
from .http_client import (  # re-exported on purpose: legacy import surface
    HEADERS,
    ResponseHandle,
    build_chat_payload,
    error_for_status,
    iter_sse_text,
    new_client,
    read_json_text,
    timeout_for,
)
from .models import (
    EngineError,
    RetryableEngineError,
    TranslateChunk,
    TranslateRequest,
)
from .prompt_builder import strip_think


class ApiError(EngineError):
    """Legacy error type. ``retryable`` is kept for callers that branch on it."""

    def __init__(self, message: str, detail: str = "", retryable: bool = False) -> None:
        super().__init__(message, detail)
        self.retryable = bool(retryable)


def _as_api_error(exc: EngineError) -> ApiError:
    return ApiError(
        exc.message,
        exc.detail,
        retryable=isinstance(exc, RetryableEngineError) or getattr(exc, "retryable", False),
    )


# --------------------------------------------------------------------- payload
def build_payload(
    model: str,
    content: str | list[dict[str, Any]],
    *,
    stream: bool,
    max_tokens: int,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """OpenAI-compatible body with thinking disabled (byte-identical to V1)."""
    return build_chat_payload(
        model,
        content,
        stream=stream,
        max_tokens=max_tokens,
        temperature=temperature,
        native=True,
    )


# ------------------------------------------------------------------- translate
def translate(
    *,
    text: str = "",
    image_data_url: str = "",
    target_lang: str,
    source_lang: str = "auto",
    instruction: str = "",
    glossary: str = "",
    model: str = DEFAULT_MODEL,
    api_base: str = API_BASE,
    timeout: float = 120.0,
    stream: bool = True,
    temperature: float = 0.0,
    cancel_event: threading.Event | None = None,
    handle: ResponseHandle | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> str:
    """One-shot translation through the Index-Translate engine.

    No cache, no rate limiting and no model fallback: those belong to the
    pipeline, and this entry point deliberately stays a single honest request
    (tests point it at a local fake server).
    """
    req = TranslateRequest(
        text=text or "",
        image_b64=(image_data_url or None),
        source_lang=source_lang or "auto",
        target_lang=target_lang,
        instruction=instruction or "",
        model=model or DEFAULT_MODEL,
        stream=bool(stream),
        use_cache=False,
        glossary_group=None,          # the glossary string is already formatted
        extra={
            "api_base": api_base or API_BASE,
            "timeout": float(timeout or 120.0),
            "temperature": float(temperature or 0.0),
            "handle": handle,
        },
    )
    engine = IndexTranslateEngine(base_url=api_base or API_BASE, timeout=float(timeout or 120.0))
    terms = [glossary] if glossary else []

    parts: list[str] = []
    try:
        for chunk in engine.translate_stream(req, terms, cancel_event or threading.Event()):
            if chunk.delta:
                parts.append(chunk.delta)
                if on_delta is not None:
                    on_delta(chunk.delta)
    except EngineError as exc:
        raise _as_api_error(exc) from exc
    return "".join(parts)


# ---------------------------------------------------------------- raw payloads
def stream_chat(
    payload: dict[str, Any],
    *,
    api_base: str = API_BASE,
    timeout: float = 120.0,
    cancel_event: threading.Event | None = None,
    handle: ResponseHandle | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> str:
    """Stream an already-built payload (legacy helper, no retry, no cache)."""
    cancel = cancel_event or threading.Event()
    url = f"{(api_base or API_BASE).rstrip('/')}/chat/completions"
    parts: list[str] = []

    try:
        with new_client(timeout) as client:
            with client.stream("POST", url, json=payload, headers=dict(HEADERS)) as response:
                if handle is not None and handle.bind(response):
                    return "".join(parts)
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", "replace")
                    raise error_for_status(response.status_code, body)

                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type and "event-stream" not in content_type:
                    text = strip_think(read_json_text(response))
                    if not text:
                        raise RetryableEngineError("服务没有返回译文，请稍后重试。")
                    if on_delta is not None:
                        on_delta(text)
                    return text

                for piece in iter_sse_text(response.iter_lines(), cancel):
                    parts.append(piece)
                    if on_delta is not None:
                        on_delta(piece)
    except EngineError as exc:
        raise _as_api_error(exc) from exc

    text = "".join(parts)
    if not text.strip() and not cancel.is_set():
        raise ApiError("服务没有返回译文，请稍后重试。", retryable=True)
    return text


def chat_once(
    payload: dict[str, Any],
    *,
    api_base: str = API_BASE,
    timeout: float = 120.0,
    cancel_event: threading.Event | None = None,
) -> str:
    """Non-streaming request with an already-built payload."""
    url = f"{(api_base or API_BASE).rstrip('/')}/chat/completions"
    body = dict(payload)
    body["stream"] = False
    try:
        with new_client(timeout) as client:
            response = client.post(url, json=body, headers=dict(HEADERS))
            if response.status_code >= 400:
                raise error_for_status(response.status_code, response.text)
            text = strip_think(read_json_text(response))
    except EngineError as exc:
        raise _as_api_error(exc) from exc
    if not text.strip():
        raise ApiError("服务没有返回译文，请稍后重试。", retryable=True)
    return text


__all__ = [
    "ApiError",
    "HEADERS",
    "ResponseHandle",
    "build_payload",
    "chat_once",
    "new_client",
    "stream_chat",
    "timeout_for",
    "translate",
]
