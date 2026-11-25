"""HTTP client for the free public Index-Translate endpoint (OpenAI compatible).

Design notes:
* `enable_thinking` is sent as ``false`` on every request — with it enabled the
  CoT text can replace the translation in the response (see call_api.py).
* SSE parsing is defensive: chunks without ``choices``, empty ``choices`` and
  non-object chunks are skipped instead of raising.
* The free service has no SLA, so transport errors / 429 / 5xx are retried once
  with a short backoff, but only before the first token has been emitted.
* Cancellation closes the live response from the UI thread, which unblocks a
  pending read.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import httpx

from .config import API_BASE, DEFAULT_MODEL
from .prompt_builder import build_image_prompt, build_prompt, strip_think

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Index-Translate-Client/1.0",
    "Accept": "text/event-stream, application/json",
}

DeltaCallback = Callable[[str], None]


class ApiError(Exception):
    """User-presentable API failure with an optional technical detail."""

    def __init__(self, message: str, detail: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.retryable = retryable


class ResponseHandle:
    """Lets the UI thread cancel a request that is blocked reading a stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._response: httpx.Response | None = None
        self._cancelled = False

    def bind(self, response: httpx.Response) -> bool:
        """Attach the live response. Returns True when cancel already happened."""
        with self._lock:
            self._response = response
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancelled


def _timeout(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=seconds, write=30.0, pool=10.0)


def _json_content(data: Any) -> str:
    """Extract message text from an OpenAI-style completion object."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict)
        )
    return content if isinstance(content, str) else ""


def _server_message(body: str) -> str:
    try:
        data = json.loads(body)
    except Exception:
        return body.strip()[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error)[:300]
        if isinstance(error, str):
            return error[:300]
        detail = data.get("detail") or data.get("message")
        if detail:
            return str(detail)[:300]
    return body.strip()[:300]


def _http_error(status: int, body: str) -> ApiError:
    server = _server_message(body)
    detail = f"HTTP {status}: {server}" if server else f"HTTP {status}"
    if status == 429:
        return ApiError("请求过于频繁，请稍后再试。", detail, retryable=True)
    if status in (401, 403):
        return ApiError("服务拒绝了请求（鉴权或访问受限）。", detail)
    if status == 404:
        return ApiError("接口地址无法访问，请检查「设置 · 接口地址」。", detail)
    if status >= 500:
        return ApiError(f"翻译服务暂时不可用（HTTP {status}），请稍后重试。", detail, retryable=True)
    return ApiError(f"翻译请求失败（HTTP {status}）。", detail)


def _transport_error(exc: Exception) -> ApiError:
    if isinstance(exc, httpx.ConnectTimeout):
        return ApiError("连接翻译服务超时，请检查网络后重试。", repr(exc), retryable=True)
    if isinstance(exc, httpx.ReadTimeout):
        return ApiError("等待译文超时，免费公网服务可能拥塞，请稍后重试。", repr(exc), retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        return _http_error(exc.response.status_code, exc.response.text)
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return ApiError("请求地址无效，请检查「设置 · 接口地址」。", repr(exc), retryable=False)
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return ApiError("无法连接到翻译服务，请检查网络或代理设置。", repr(exc), retryable=True)
    return ApiError("翻译请求失败，请稍后重试。", repr(exc), retryable=True)


def _new_client(timeout: float) -> httpx.Client:
    """Build a client, tolerating broken proxy environment variables.

    Some Windows setups export a NO_PROXY value httpx cannot parse (e.g. "::1"),
    which raises InvalidURL at construction time. The API is a public domestic
    endpoint, so falling back to a direct connection is the right behaviour.
    """
    kwargs = {"timeout": _timeout(timeout), "follow_redirects": True}
    try:
        return httpx.Client(trust_env=True, **kwargs)
    except Exception:
        return httpx.Client(trust_env=False, **kwargs)


def new_client(timeout: float) -> httpx.Client:
    """Public wrapper: the local proxy server needs the same proxy fallback."""
    return _new_client(timeout)


def build_payload(
    model: str,
    content: str | list[dict[str, Any]],
    *,
    stream: bool,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
        # Critical: thinking must stay off or CoT text can replace the translation.
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _endpoint(api_base: str) -> str:
    return f"{(api_base or API_BASE).rstrip('/')}/chat/completions"


def stream_chat(
    payload: dict[str, Any],
    *,
    api_base: str,
    timeout: float,
    cancel_event: threading.Event | None = None,
    handle: ResponseHandle | None = None,
    on_delta: DeltaCallback,
) -> str:
    """Run a streaming request; returns the accumulated translation."""
    cancel_event = cancel_event or threading.Event()
    handle = handle or ResponseHandle()
    url = _endpoint(api_base)
    emitted = ""
    last_error: ApiError | None = None

    for attempt in (0, 1):
        try:
            with _new_client(timeout) as client:
                with client.stream("POST", url, json=payload, headers=HEADERS) as response:
                    if handle.bind(response):
                        return emitted
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", "replace")
                        raise _http_error(response.status_code, body)

                    content_type = response.headers.get("content-type", "")
                    if "application/json" in content_type and "event-stream" not in content_type:
                        # Server ignored stream=true and answered with one JSON body.
                        raw = response.read().decode("utf-8", "replace")
                        try:
                            data = json.loads(raw)
                        except Exception:
                            raise ApiError("服务返回了无法解析的响应。", raw[:300], retryable=True)
                        text = strip_think(_json_content(data))
                        if not text:
                            raise ApiError("服务没有返回译文，请稍后重试。", raw[:300], retryable=True)
                        on_delta(text)
                        return text

                    for line in response.iter_lines():
                        if cancel_event.is_set():
                            return emitted
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if not body or body == "[DONE]":
                            if body == "[DONE]":
                                break
                            continue
                        try:
                            chunk = json.loads(body)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        if "error" in chunk:
                            raise ApiError(
                                "翻译服务返回错误，请稍后重试。",
                                _server_message(body),
                                retryable=False,
                            )
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            emitted += piece
                            on_delta(piece)

            if cancel_event.is_set():
                return emitted
            if not emitted.strip():
                raise ApiError("服务没有返回译文，请稍后重试。", "", retryable=True)
            return emitted

        except ApiError as exc:
            last_error = exc
            if cancel_event.is_set():
                return emitted
            if emitted or not exc.retryable or attempt == 1:
                raise
        except Exception as exc:  # httpx transport errors and anything unexpected
            if cancel_event.is_set():
                return emitted
            last_error = _transport_error(exc)
            if emitted or attempt == 1:
                raise last_error

        if cancel_event.is_set():
            return emitted
        time.sleep(1.2)

    raise last_error or ApiError("翻译请求失败，请稍后重试。")


def chat_once(
    payload: dict[str, Any],
    *,
    api_base: str,
    timeout: float,
    cancel_event: threading.Event | None = None,
) -> str:
    """Non-streaming request used when the user turns streaming off."""
    cancel_event = cancel_event or threading.Event()
    if cancel_event.is_set():
        return ""
    url = _endpoint(api_base)
    last_error: ApiError | None = None

    for attempt in (0, 1):
        try:
            with _new_client(timeout) as client:
                response = client.post(url, json=payload, headers=HEADERS)
                if response.status_code >= 400:
                    raise _http_error(response.status_code, response.text)
                try:
                    data = response.json()
                except Exception:
                    raise ApiError("服务返回了无法解析的响应。", response.text[:300], retryable=True)
                text = strip_think(_json_content(data))
                if not text:
                    raise ApiError("服务没有返回译文，请稍后重试。", response.text[:300], retryable=True)
                return text
        except ApiError as exc:
            last_error = exc
            if cancel_event.is_set():
                return ""
            if not exc.retryable or attempt == 1:
                raise
        except Exception as exc:
            if cancel_event.is_set():
                return ""
            last_error = _transport_error(exc)
            if attempt == 1:
                raise last_error
        time.sleep(1.2)

    raise last_error or ApiError("翻译请求失败，请稍后重试。")


def translate(
    *,
    text: str,
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
    on_delta: DeltaCallback,
) -> str:
    """Translate text or an image; returns the full translation."""
    cancel_event = cancel_event or threading.Event()
    if image_data_url:
        content: str | list[dict[str, Any]] = build_image_prompt(
            image_data_url,
            target_lang,
            source_lang,
            ref_text=text,
            instruction=instruction,
            glossary=glossary,
        )
        max_tokens = 2048
    else:
        content = build_prompt(
            text,
            target_lang,
            source_lang,
            instruction=instruction,
            glossary=glossary,
        )
        # Generous but bounded: the endpoint truncates anything above its own cap.
        max_tokens = min(4096, max(1024, int(len(text) * 1.25) + 256))

    payload = build_payload(
        model,
        content,
        stream=stream,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    if stream:
        return stream_chat(
            payload,
            api_base=api_base,
            timeout=timeout,
            cancel_event=cancel_event,
            handle=handle,
            on_delta=on_delta,
        )

    text_result = chat_once(
        payload,
        api_base=api_base,
        timeout=timeout,
        cancel_event=cancel_event,
    )
    if text_result:
        on_delta(text_result)
    return text_result

