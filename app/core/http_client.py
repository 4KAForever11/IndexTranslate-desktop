"""Shared HTTP plumbing: client construction, SSE parsing, error mapping.

Both engines and the legacy ``api_client`` compatibility layer sit on this
module, so there is exactly one place that knows:

* how to build an ``httpx.Client`` on machines with a broken ``NO_PROXY`` value;
* how to read an OpenAI-style SSE stream defensively;
* how an HTTP status or transport exception becomes a user-facing error, and
  whether it is worth retrying with another model.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Iterator

import httpx

from .models import EngineError, RetryableEngineError

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Index-Translate-Client/1.0",
    "Accept": "text/event-stream, application/json",
}

#: Statuses that mean "this model or host is unhappy — another one may work".
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ResponseHandle:
    """Lets the UI thread cancel a request blocked reading a stream.

    Setting a threading.Event only stops the *loop*; a response whose socket
    stalled mid-chunk stays blocked inside ``iter_lines``. Closing the live
    response from the other thread unblocks it.
    """

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


def timeout_for(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=seconds, write=30.0, pool=10.0)


def new_client(timeout: float) -> httpx.Client:
    """Build a client, tolerating broken proxy environment variables.

    Some Windows setups export a ``NO_PROXY`` value httpx cannot parse (e.g.
    ``::1``), which raises ``InvalidURL`` at construction time. The translation
    endpoint is a public domestic service, so connecting directly is right.
    """
    kwargs = {"timeout": timeout_for(timeout), "follow_redirects": True}
    try:
        return httpx.Client(trust_env=True, **kwargs)
    except Exception:
        return httpx.Client(trust_env=False, **kwargs)


def json_content(data: Any) -> str:
    """Extract message text from an OpenAI-style completion object."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return content if isinstance(content, str) else ""


def server_message(body: str) -> str:
    """Best-effort human message out of an upstream error body."""
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


def error_for_status(status: int, body: str) -> EngineError:
    """Map an HTTP failure to a Chinese message, marking transient ones."""
    server = server_message(body)
    detail = f"HTTP {status}: {server}" if server else f"HTTP {status}"
    if status == 429:
        return RetryableEngineError("请求过于频繁，请稍后再试。", status, detail)
    if status in (401, 403):
        return EngineError("服务拒绝了请求（鉴权或访问受限）。", detail, status)
    if status == 404:
        return EngineError("接口地址无法访问，请检查「设置 · 接口地址」。", detail, status)
    if status >= 500:
        return RetryableEngineError(
            f"翻译服务暂时不可用（HTTP {status}），请稍后重试。", status, detail
        )
    if status in RETRYABLE_STATUS:
        return RetryableEngineError(f"翻译请求失败（HTTP {status}），正在重试。", status, detail)
    return EngineError(f"翻译请求失败（HTTP {status}）。", detail, status)


def transport_error(exc: Exception) -> EngineError:
    """Map httpx transport exceptions; most are worth another attempt."""
    if isinstance(exc, httpx.ConnectTimeout):
        return RetryableEngineError("连接翻译服务超时，请检查网络后重试。", None, repr(exc))
    if isinstance(exc, httpx.ReadTimeout):
        return RetryableEngineError(
            "等待译文超时，免费公网服务可能拥塞，请稍后重试。", None, repr(exc)
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return error_for_status(exc.response.status_code, exc.response.text)
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return EngineError("请求地址无效，请检查「设置 · 接口地址」。", repr(exc))
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return RetryableEngineError("无法连接到翻译服务，请检查网络或代理设置。", None, repr(exc))
    return RetryableEngineError("翻译请求失败，请稍后重试。", None, repr(exc))


def build_chat_payload(
    model: str,
    content: str | list[dict[str, Any]],
    *,
    stream: bool,
    max_tokens: int,
    temperature: float = 0.0,
    native: bool = True,
) -> dict[str, Any]:
    """OpenAI chat-completions body.

    ``native`` adds the two fields the Index-Translate service needs and that a
    generic OpenAI-compatible server would reject: ``chat_template_kwargs`` with
    ``enable_thinking: false`` (with thinking on, the CoT text can replace the
    translation) and an explicit ``temperature``.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "stream": stream,
    }
    payload["temperature"] = temperature
    if native:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def iter_sse_text(lines: Iterator[str], cancel_event: threading.Event) -> Iterator[str]:
    """Yield content deltas from an OpenAI-style SSE line stream.

    Chunks without ``choices``, empty ``choices`` and non-object chunks are
    skipped rather than raising: the free service occasionally interleaves
    keep-alive or usage frames.
    """
    for line in lines:
        if cancel_event.is_set():
            return
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            return
        if not body:
            continue
        try:
            chunk = json.loads(body)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if "error" in chunk:
            raise EngineError("翻译服务返回错误，请稍后重试。", server_message(body))
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            yield piece


def read_json_text(response: httpx.Response) -> str:
    """Parse a non-streaming completion body into plain text."""
    raw = response.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except Exception:
        raise RetryableEngineError("服务返回了无法解析的响应。", None, raw[:300])
    if isinstance(data, dict) and "error" in data:
        raise EngineError("翻译服务返回错误，请稍后重试。", server_message(raw))
    return json_content(data)
