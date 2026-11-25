"""Core test suite — run with `python -m unittest discover -s tests -v`."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import call_api  # reference implementation, parity baseline

from app.core import api_client
from app.core.glossary_db import GlossaryDB
from app.core.history_db import HistoryDB
from app.core.image_utils import (
    ImagePayload,
    fetch_remote_image,
    make_thumbnail_from_bytes,
    prepare_image,
)
from app.core.prompt_builder import build_prompt, parse_glossary_terms, strip_think
from app.core.proxy_server import ProxyServer


class PromptParityTests(unittest.TestCase):
    """The public model is prompt-sensitive: every byte must match call_api.py."""

    CASES = [
        dict(text="你好，世界。", target_lang="en"),
        dict(text="  hello  ", target_lang="zh", source_lang="en"),
        dict(text="Open source rocks.", target_lang="zh", source_lang="en", genre="技术文档"),
        dict(text="译文要简洁", target_lang="ja", instruction="保持简洁\n1. 不要解释"),
        dict(text="光遇", target_lang="en", glossary="光遇→Sky: Children of the Light、星穹铁道→Honkai: Star Rail"),
        dict(text="A", target_lang="de", instruction="【硬性要求】保留 Markdown"),
        dict(
            text='{"title": "你好", "body": "今天天晴"}',
            target_lang="en",
            instruction="保持 JSON 结构",
        ),
        dict(text="多语言", target_lang="zh", glossary="多语言→multilingual", instruction="2. 使用正式语气"),
    ]

    def test_matches_reference_implementation(self) -> None:
        for case in self.CASES:
            with self.subTest(case=case):
                self.assertEqual(build_prompt(**case), call_api.build_prompt(**case))

    def test_unknown_target_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_prompt("x", "klingon")

    def test_strip_think_matches(self) -> None:
        samples = ["plain", "<think>hmm</think>答案", "答案</think>多余", "  pad  "]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(strip_think(sample), call_api.strip_think(sample))


class GlossaryParsingTests(unittest.TestCase):
    def test_delimited_pairs_match_reference(self) -> None:
        samples = [
            "光遇→Sky、原神→Genshin",
            "光遇: Sky, 原神：Genshin",
            "光遇->Sky\n原神->Genshin",
            "single",
            "",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(parse_glossary_terms(sample), call_api.parse_glossary_terms(sample))

    def test_inline_json(self) -> None:
        self.assertEqual(parse_glossary_terms('{"光遇": "Sky"}'), ["光遇→Sky"])

    def test_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "terms.json"
            path.write_text(json.dumps({"光遇": "Sky"}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(parse_glossary_terms(str(path)), ["光遇→Sky"])


class ImageTests(unittest.TestCase):
    def test_large_image_is_downscaled(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.png"
            Image.new("RGB", (4200, 2100), "white").save(path)
            payload = prepare_image(str(path))
            self.assertIsInstance(payload, ImagePayload)
            self.assertTrue(payload.data_url.startswith("data:image/"))
            self.assertLessEqual(max(payload.width, payload.height), 2048)
            self.assertGreater(payload.byte_size, 0)

    def test_remote_url_is_passthrough(self) -> None:
        payload = prepare_image("https://example.com/a.png")
        self.assertEqual(payload.data_url, "https://example.com/a.png")
        self.assertTrue(payload.source.startswith("https://"))

    def test_fetch_remote_image_downloads_and_encodes(self) -> None:
        """The public endpoint rejects remote references, so we must download first."""
        from PIL import Image

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            source = Path(tmp) / "remote.png"
            Image.new("RGB", (640, 200), "white").save(source)

            class _Handler(_StaticFileHandler):
                directory = str(Path(tmp))

            server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)

            url = f"http://127.0.0.1:{server.server_port}/remote.png"
            payload = fetch_remote_image(url)

            self.assertTrue(payload.data_url.startswith("data:image/"))
            self.assertEqual(payload.width, 640)
            self.assertEqual(payload.height, 200)
            self.assertEqual(payload.display_name, "remote.png")

    def test_fetch_remote_image_rejects_non_http(self) -> None:
        with self.assertRaises(ValueError):
            fetch_remote_image("ftp://example.com/a.png")

    def test_fetch_remote_image_reports_download_failure(self) -> None:
        with self.assertRaises(ValueError):
            fetch_remote_image("http://127.0.0.1:9/never.png")

    def test_make_thumbnail_from_bytes_accepts_data_url(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.png"
            Image.new("RGB", (500, 250), "blue").save(path)
            data_url = prepare_image(str(path)).data_url

            thumb = make_thumbnail_from_bytes(data_url)
            self.assertIsInstance(thumb, bytes)
            self.assertGreater(len(thumb), 0)
            self.assertIsNone(make_thumbnail_from_bytes("not-a-data-url"))


class DatabaseTests(unittest.TestCase):
    def test_history_roundtrip_and_trim(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = HistoryDB(Path(tmp) / "history.db")
            self.addCleanup(db.close)
            for index in range(5):
                db.add(
                    kind="text",
                    source_text=f"src {index}",
                    result_text=f"dst {index}",
                    source_lang="zh",
                    target_lang="en",
                    model="Index-Translate-2B",
                    image_thumb=None,
                    elapsed_ms=100 * index,
                    keep=3,
                )
            self.assertEqual(db.count(""), 3)
            rows = db.search("", limit=10)
            self.assertEqual(rows[0].source_text, "src 4")
            self.assertTrue(db.search("src 4"))
            self.assertEqual(len(db.search("nothing matches")), 0)
            db.clear()
            self.assertEqual(db.count(""), 0)

    def test_glossary_crud_and_import(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = GlossaryDB(Path(tmp) / "glossary.db")
            self.addCleanup(db.close)
            db.upsert("光遇", "Sky")
            db.upsert("光遇", "Sky: Children of the Light")
            self.assertEqual(db.count(), 1)
            self.assertEqual(db.get("光遇"), "Sky: Children of the Light")
            added = db.import_lines(["原神 => Genshin", "# comment", "", "星穹铁道\tHonkai: Star Rail"])
            self.assertEqual(added, 2)
            self.assertEqual(db.prompt_string(), "光遇→Sky: Children of the Light、原神→Genshin、星穹铁道→Honkai: Star Rail")
            self.assertIn("原神\tGenshin", db.export_text())


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Test server that ignores client disconnects instead of printing tracebacks."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return
        super().handle_error(request, client_address)


class _StaticFileHandler(BaseHTTPRequestHandler):
    """Serves one PNG from ``handler.directory`` so URL input can be tested."""

    protocol_version = "HTTP/1.1"
    directory = "."

    def log_message(self, *args) -> None:  # silence
        pass

    def do_GET(self) -> None:  # noqa: N802
        name = self.path.split("?")[0].lstrip("/")
        target = Path(self.directory) / name
        if not target.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _FakeSSEHandler(BaseHTTPRequestHandler):
    """Minimal upstream that streams two SSE deltas, like the real service."""

    protocol_version = "HTTP/1.1"
    payloads = ["Hello", " world"]

    def log_message(self, *args) -> None:  # silence
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        assert b"enable_thinking" in body, "payload must disable thinking"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for piece in self.payloads:
            chunk = "data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n"
            self._write_chunk(chunk)
        self._write_chunk("data: [DONE]\n\n")
        try:
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass

    def _write_chunk(self, text: str) -> None:
        data = text.encode("utf-8")
        try:
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        except OSError:
            # The client may close right after [DONE]; nothing to report.
            pass


class StreamingTests(unittest.TestCase):
    def test_stream_chat_reads_sse(self) -> None:
        server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _FakeSSEHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            pieces: list[str] = []
            result = api_client.translate(
                text="你好",
                target_lang="en",
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
                stream=True,
                timeout=10,
                on_delta=pieces.append,
            )
            self.assertEqual(result, "Hello world")
            self.assertEqual(pieces, ["Hello", " world"])
        finally:
            server.shutdown()
            server.server_close()

    def test_payload_forces_thinking_off(self) -> None:
        payload = api_client.build_payload(
            "Index-Translate-35B-A3B", "hi", stream=True, max_tokens=1024, temperature=0.0
        )
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(payload["temperature"], 0.0)
        self.assertTrue(payload["stream"])


class ProxyTests(unittest.TestCase):
    """The local OpenAI-compatible proxy is a user-facing feature; keep it honest."""

    def _start(self, handler):
        upstream = _QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        return upstream

    def test_proxy_relays_stream_and_models(self) -> None:
        import httpx

        upstream = self._start(_FakeSSEHandler)
        proxy = ProxyServer(
            port=0,
            api_base=f"http://127.0.0.1:{upstream.server_port}/v1",
            model="Index-Translate-2B",
            timeout=10,
        )
        proxy.start()
        self.addCleanup(proxy.stop)
        self.assertTrue(proxy.running)

        pieces: list[str] = []
        with httpx.Client(trust_env=False, timeout=10) as client:
            with client.stream(
                "POST",
                proxy.url + "/chat/completions",
                json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
            ) as response:
                self.assertEqual(response.status_code, 200)
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    pieces.append(json.loads(chunk)["choices"][0]["delta"]["content"])
            models = client.get(proxy.url + "/models").json()

        self.assertEqual("".join(pieces), "Hello world")
        self.assertIn("Index-Translate-35B-A3B", [item["id"] for item in models["data"]])
        proxy.stop()
        self.assertFalse(proxy.running)


if __name__ == "__main__":
    unittest.main(verbosity=2)
