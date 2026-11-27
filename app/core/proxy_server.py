"""A small OpenAI-compatible proxy that makes the free upstream service usable.

Immersive Translate (or any OpenAI-compatible client) points at
``http://127.0.0.1:8080/v1`` and gets, on top of plain forwarding:

* **throttling** — bounded concurrency plus a token bucket, so a whole web page
  of paragraphs does not stampede the upstream limiter;
* **caching** — identical source text + parameters replay instantly from SQLite;
* **glossary injection** — the user's term list is matched against the text and
  appended to the prompt, so browser translations honour the same glossary as
  the desktop client;
* **model fallback** — 429/5xx/timeout walks 35B → 9B → 2B and finally an
  optional user-configured OpenAI-compatible endpoint.

Only ``GET /v1/models``, ``GET /health`` and ``POST /v1/chat/completions`` are
implemented; the upstream path is preserved so other suffixes still work.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from .api_client import new_client
from .config import API_BASE, DEFAULT_MODEL, MODELS, data_dir
from .proxy_cache import ProxyCache
from .proxy_throttle import RequestGate
from .proxy_utils import (
    completion_json,
    extract_json_text,
    extract_stream_text,
    inject_glossary,
    make_cache_key,
    model_chain,
    sse_from_text,
)

# Statuses that mean "this model/host is unhappy, try the next one".
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

_GLOSSARY_TTL = 5.0


def default_policy() -> dict[str, Any]:
    return {
        "max_concurrency": 4,
        "qps": 3.0,
        "burst": 6,
        "queue_timeout": 90.0,
        "cache_enabled": True,
        "cache_size": 2000,
        "cache_ttl": 7.0,
        "inject_glossary": True,
        "fallback_enabled": True,
        "fallback_models": "",
        "fallback_api_base": "",
        "fallback_model": "",
        "fallback_api_key": "",
    }


class _ProxyHandler(BaseHTTPRequestHandler):
    server_version = "IndexTranslateDesktopProxy/2.0"
    protocol_version = "HTTP/1.1"

    # Silence the default stderr access log; the proxy runs in the background.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # ------------------------------------------------------------- utilities
    @property
    def _state(self) -> dict[str, Any]:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def _policy(self) -> dict[str, Any]:
        return self._state["policy"]

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _error(self, status: int, message: str, kind: str = "upstream_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": kind}})

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    # ----------------------------------------------------------------- verbs
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path.endswith("/models"):
            data = [
                {
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "bilibili-index-translate",
                }
                for model_id, _ in MODELS
            ]
            self._send_json(200, {"object": "list", "data": data})
            return
        if path in ("", "/health", "/v1/health", "/v1/stats", "/stats"):
            self._send_json(200, self._stats_payload())
            return
        self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})

    def _stats_payload(self) -> dict[str, Any]:
        cache: ProxyCache | None = self._state.get("cache")
        gate: RequestGate | None = self._state.get("gate")
        return {
            "status": "ok",
            "upstream": self._state["api_base"],
            "cache": cache.stats() if cache is not None else {},
            "gate": gate.stats() if gate is not None else {},
        }

    # -------------------------------------------------------------- post flow
    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json(
                404, {"error": {"message": "Not found", "type": "invalid_request_error"}}
            )
            return

        raw = self._read_body()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send_json(
                400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}
            )
            return
        if not isinstance(payload, dict):
            self._send_json(
                400,
                {"error": {"message": "Body must be a JSON object", "type": "invalid_request_error"}},
            )
            return

        policy = self._policy
        payload.setdefault("model", self._state["model"] or DEFAULT_MODEL)
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload.setdefault("temperature", 0.0)
        want_stream = bool(payload.get("stream"))

        self._inject_glossary(payload)

        cache: ProxyCache | None = self._state.get("cache")
        cache_key = make_cache_key(payload) if cache is not None else ""
        if cache is not None and policy.get("cache_enabled", True):
            hit = cache.get(cache_key)
            if hit and hit.get("content"):
                if gate := self._state.get("gate"):
                    gate.note_served()
                self._replay(str(hit["content"]), str(hit.get("model") or payload["model"]), want_stream)
                return

        gate: RequestGate | None = self._state.get("gate")
        if gate is None:  # pragma: no cover - only before start()
            self._forward(payload, want_stream, cache, cache_key)
            return

        with gate.slot() as admitted:
            if not admitted:
                self._error(
                    503,
                    "本地代理排队超时：请降低并发或稍后重试（设置 → 代理优化）。",
                    "rate_limited",
                )
                return
            self._forward(payload, want_stream, cache, cache_key)

    # --------------------------------------------------------------- helpers
    def _inject_glossary(self, payload: dict[str, Any]) -> None:
        if not self._policy.get("inject_glossary", True):
            return
        glossary = self._state.get("glossary")
        if glossary is None:
            return
        now = time.monotonic()
        cached = self._state.get("glossary_cache")
        if cached is None or now - float(cached[0]) > _GLOSSARY_TTL:
            try:
                pairs = glossary.all_enabled()
            except Exception:
                pairs = []
            cached = (now, pairs)
            self._state["glossary_cache"] = cached
        pairs = cached[1]
        if not pairs:
            return
        injected = inject_glossary(payload, pairs)
        if injected:
            self._state["last_glossary_count"] = injected

    def _attempts(self, payload: dict[str, Any]) -> list[tuple[str, str, str, bool]]:
        """``(api_base, model, api_key, native)`` tuples in fallback order."""
        policy = self._policy
        primary_base = str(self._state["api_base"]).rstrip("/")
        if not policy.get("fallback_enabled", True):
            return [(primary_base, str(payload["model"]), "", True)]

        known = [model_id for model_id, _ in MODELS]
        chain = model_chain(str(payload["model"]), str(policy.get("fallback_models") or ""), known)
        attempts = [(primary_base, model, "", True) for model in chain]

        fb_base = str(policy.get("fallback_api_base") or "").strip().rstrip("/")
        if fb_base:
            fb_model = str(policy.get("fallback_model") or "").strip() or str(payload["model"])
            attempts.append((fb_base, fb_model, str(policy.get("fallback_api_key") or ""), False))
        return attempts

    def _forward(
        self,
        payload: dict[str, Any],
        want_stream: bool,
        cache: ProxyCache | None,
        cache_key: str,
    ) -> None:
        attempts = self._attempts(payload)
        timeout = float(self._state["timeout"])
        last_status = 502
        last_message = "Upstream request failed"

        for api_base, model, api_key, native in attempts:
            body = dict(payload)
            body["model"] = model
            if not native:
                # The fallback endpoint is a normal OpenAI server: keep only
                # the fields it understands.
                body.pop("chat_template_kwargs", None)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Index-Translate-Client/1.0",
                "Accept": "text/event-stream, application/json",
            }
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            url = f"{api_base}/chat/completions"

            try:
                with new_client(timeout) as client:
                    with client.stream("POST", url, json=body, headers=headers) as response:
                        if response.status_code >= 400:
                            error_body = response.read()
                            last_status = response.status_code
                            last_message = _upstream_message(error_body, response.status_code)
                            if response.status_code not in _RETRY_STATUS:
                                self._relay_raw(
                                    response.status_code,
                                    error_body,
                                    response.headers.get("content-type"),
                                )
                                return
                            continue
                        self._relay_success(
                            response, want_stream, model, cache, cache_key
                        )
                        return
            except httpx.HTTPError as exc:
                last_status = 502
                last_message = f"上游请求失败：{exc.__class__.__name__}"
                continue
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

        self._error(last_status if last_status >= 400 else 502, last_message)

    def _relay_raw(self, status: int, body: bytes, content_type: str | None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _relay_success(
        self,
        response: httpx.Response,
        want_stream: bool,
        model: str,
        cache: ProxyCache | None,
        cache_key: str,
    ) -> None:
        content_type = response.headers.get("content-type", "application/json")
        is_sse = "event-stream" in content_type

        if want_stream and is_sse:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self._cors()
            self.end_headers()
            buffered = bytearray()
            for chunk in response.iter_raw():
                if not chunk:
                    continue
                buffered.extend(chunk)
                try:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    break
            else:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            self._store(cache, cache_key, extract_stream_text(bytes(buffered)), model)
            return

        body = response.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        written = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            written = False
        if written:
            text = extract_json_text(body) if "json" in (content_type or "") else ""
            self._store(cache, cache_key, text, model)

    def _store(self, cache: ProxyCache | None, key: str, text: str, model: str) -> None:
        if cache is None or not key or not text.strip():
            return
        if not self._policy.get("cache_enabled", True):
            return
        cache.put(key, {"content": text, "model": model}, model)

    def _replay(self, text: str, model: str, want_stream: bool) -> None:
        if want_stream:
            body = sse_from_text(text, model)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Proxy-Cache", "HIT")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        body = json.dumps(completion_json(text, model), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Proxy-Cache", "HIT")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def _upstream_message(body: bytes, status: int) -> str:
    """Best-effort human message out of an upstream error body."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"上游返回 HTTP {status}"
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"上游返回 HTTP {status}：{error['message']}"
        if isinstance(error, str) and error:
            return f"上游返回 HTTP {status}：{error}"
        if parsed.get("message"):
            return f"上游返回 HTTP {status}：{parsed['message']}"
    return f"上游返回 HTTP {status}"


class _ProxyServer_(ThreadingHTTPServer):
    """A ThreadingHTTPServer that ignores client-side disconnects."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class ProxyServer:
    """Owns the background HTTP server thread and its policy objects."""

    def __init__(
        self,
        port: int = 8080,
        api_base: str = API_BASE,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        *,
        glossary: Any = None,
        cache: ProxyCache | None = None,
        policy: dict[str, Any] | None = None,
    ) -> None:
        self.port = int(port)
        self._api_base = api_base
        self._model = model
        self._timeout = timeout
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._cache = cache
        self._owns_cache = cache is None
        self._policy = {**default_policy(), **(policy or {})}
        self._gate = self._make_gate()
        self._state: dict[str, Any] = {
            "api_base": self._api_base,
            "model": self._model,
            "timeout": self._timeout,
            "policy": self._policy,
            "gate": self._gate,
            "cache": None,
            "glossary": glossary,
            "glossary_cache": None,
        }

    # ------------------------------------------------------------- lifecycle
    def _make_gate(self) -> RequestGate:
        policy = self._policy
        return RequestGate(
            max_concurrency=int(policy.get("max_concurrency") or 4),
            qps=float(policy.get("qps") or 0),
            burst=int(policy.get("burst") or 6),
            queue_timeout=float(policy.get("queue_timeout") or 90),
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def policy(self) -> dict[str, Any]:
        return dict(self._policy)

    def set_glossary(self, glossary: Any) -> None:
        self._state["glossary"] = glossary
        self._state["glossary_cache"] = None

    def apply_policy(self, **changes: Any) -> None:
        """Hot-update limits; the gate is rebuilt but in-flight calls finish."""
        self._policy.update({key: value for key, value in changes.items() if key in self._policy})
        self._gate = self._make_gate()
        self._state["gate"] = self._gate
        if self._cache is not None:
            self._cache.max_entries = int(self._policy.get("cache_size") or 0)
            self._cache.ttl_seconds = float(self._policy.get("cache_ttl") or 0) * 86400.0

    def stats(self) -> dict[str, Any]:
        return {
            "cache": self._cache.stats() if self._cache is not None else {},
            "gate": self._gate.stats(),
        }

    def clear_cache(self) -> int:
        return self._cache.clear() if self._cache is not None else 0

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        if self._cache is None:
            policy = self._policy
            self._cache = ProxyCache(
                data_dir() / "proxy_cache.db",
                max_entries=int(policy.get("cache_size") or 2000),
                ttl_days=float(policy.get("cache_ttl") or 7),
            )
        self._state["cache"] = self._cache
        try:
            httpd = _ProxyServer_(("127.0.0.1", self.port), _ProxyHandler)
        except OSError as exc:
            raise RuntimeError(f"端口 {self.port} 无法监听：{exc}") from exc
        self.port = int(httpd.server_port)  # port=0 means "pick a free port"
        httpd.state = self._state  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="index-translate-proxy", daemon=True
        )
        self._thread.start()
        return self.url

    def update(self, *, api_base: str | None = None, model: str | None = None) -> None:
        if api_base:
            self._api_base = api_base
            self._state["api_base"] = api_base
        if model:
            self._model = model
            self._state["model"] = model

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        if self._owns_cache and self._cache is not None:
            self._cache.close()
            self._cache = None
            self._state["cache"] = None
