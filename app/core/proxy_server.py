"""A tiny local OpenAI-compatible proxy.

Immersive Translate (and any other OpenAI-compatible client) can point at
``http://127.0.0.1:8080/v1`` instead of the remote service, which gives two
benefits: the ``enable_thinking`` flag is always injected, and the client never
needs to know the upstream host.

Only ``GET /v1/models`` and ``POST /v1/chat/completions`` are implemented; the
upstream path is preserved so a client using any other suffix still works.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from .api_client import new_client
from .config import API_BASE, DEFAULT_MODEL, MODELS


class _ProxyHandler(BaseHTTPRequestHandler):
    server_version = "IndexTranslateDesktopProxy/1.0"
    protocol_version = "HTTP/1.1"

    # Silence the default stderr access log; the proxy runs in the background.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # ------------------------------------------------------------- utilities
    @property
    def _state(self) -> dict[str, Any]:
        return self.server.state  # type: ignore[attr-defined]

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
        self.wfile.write(body)

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
        if self.path.rstrip("/").endswith("/models"):
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
        if self.path.rstrip("/") in ("", "/health", "/v1/health"):
            self._send_json(200, {"status": "ok", "upstream": self._state["api_base"]})
            return
        self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})

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
                400, {"error": {"message": "Body must be a JSON object", "type": "invalid_request_error"}}
            )
            return

        payload.setdefault("model", self._state["model"] or DEFAULT_MODEL)
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload.setdefault("temperature", 0.0)
        want_stream = bool(payload.get("stream"))

        url = f"{self._state['api_base'].rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Index-Translate-Client/1.0",
            "Accept": "text/event-stream, application/json",
        }

        try:
            with new_client(self._state["timeout"]) as client:
                with client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = response.read()
                        self.send_response(response.status_code)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self._cors()
                        self.end_headers()
                        self.wfile.write(body)
                        return

                    content_type = response.headers.get("content-type", "application/json")
                    is_sse = "event-stream" in content_type

                    if want_stream and is_sse:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Transfer-Encoding", "chunked")
                        self._cors()
                        self.end_headers()
                        for chunk in response.iter_raw():
                            if not chunk:
                                continue
                            try:
                                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                                self.wfile.write(chunk)
                                self.wfile.write(b"\r\n")
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                                return
                        try:
                            self.wfile.write(b"0\r\n\r\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                            pass
                        return

                    body = response.read()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type or "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self._cors()
                    self.end_headers()
                    self.wfile.write(body)

        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except httpx.HTTPError as exc:
            self._send_json(
                502,
                {
                    "error": {
                        "message": f"Upstream request failed: {exc.__class__.__name__}",
                        "type": "upstream_error",
                    }
                },
            )


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
    """Owns the background HTTP server thread."""

    def __init__(self, port: int = 8080, api_base: str = API_BASE, model: str = DEFAULT_MODEL, timeout: float = 120.0) -> None:
        self.port = int(port)
        self._api_base = api_base
        self._model = model
        self._timeout = timeout
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        try:
            httpd = _ProxyServer_(("127.0.0.1", self.port), _ProxyHandler)
        except OSError as exc:
            raise RuntimeError(f"端口 {self.port} 无法监听：{exc}") from exc
        self.port = int(httpd.server_port)  # port=0 means "pick a free port"
        httpd.state = {  # type: ignore[attr-defined]
            "api_base": self._api_base,
            "model": self._model,
            "timeout": self._timeout,
        }
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="index-translate-proxy", daemon=True
        )
        self._thread.start()
        return self.url

    def update(self, *, api_base: str | None = None, model: str | None = None) -> None:
        if self._httpd is None:
            return
        state = self._httpd.state  # type: ignore[attr-defined]
        if api_base:
            state["api_base"] = api_base
        if model:
            state["model"] = model

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
