"""Tests for the V2 translation foundation (pipeline / engines / limiter).

These tests are deliberately end-to-end at the HTTP boundary: a tiny local
server stands in for the real service so the SSE parsing, the retry/fallback
decision and the rate limiter are exercised exactly as they run in the app.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import call_api  # reference implementation, parity baseline

from app.core.cache_db import TranslationCacheDB
from app.core.engines import EngineRegistry, IndexTranslateEngine
from app.core.models import RetryableEngineError, TranslateChunk, TranslateRequest
from app.core.pipeline import TranslationPipeline
from app.core.rate_limiter import RateLimiter
from app.core.term_match import term_hits


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        pass


class _FlakyHandler(BaseHTTPRequestHandler):
    """Behaves differently depending on the requested model."""

    protocol_version = "HTTP/1.1"
    mode = "ok"
    delay = 0.0
    calls: list[str] = []
    active = 0
    peak = 0
    _lock = threading.Lock()

    def log_message(self, fmt, *args) -> None:  # noqa: D102
        pass

    # ------------------------------------------------------------------ helpers
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, pieces: list[str]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send_chunk(text: str) -> None:
            payload = text.encode()
            self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()

        for piece in pieces:
            frame = json.dumps(
                {"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False
            )
            send_chunk(f"data: {frame}\n\n")
        send_chunk("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # ---------------------------------------------------------------------- do
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) or b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}

        model = str(body.get("model") or "")
        with self._lock:
            type(self).calls.append(model)
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)

        try:
            if self.delay:
                time.sleep(self.delay)

            mode = type(self).mode
            if mode == "429_then_ok" and "35B" in model:
                self._json(
                    429,
                    {"error": {"message": "rate limited", "type": "rate_limit_error"}},
                )
                return

            if mode == "midstream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                frame = json.dumps({"choices": [{"delta": {"content": "Half a sen"}}]})
                payload = f"data: {frame}\n\n".encode()
                self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()
                # Drop the connection mid-stream: the client must not retry.
                self.close_connection = True
                return

            self._sse(["Hello", " world"])
        finally:
            with self._lock:
                type(self).active -= 1


class _PipelineBase(unittest.TestCase):
    mode = "ok"
    delay = 0.0

    def setUp(self) -> None:
        handler = type("_Handler", (_FlakyHandler,), {})
        handler.mode = self.mode
        handler.delay = self.delay
        handler.calls = []
        handler.active = 0
        handler.peak = 0
        self.handler = handler

        self.server = _QuietServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"

    def make_pipeline(self, *, rate=None, chain=None, cache=None) -> TranslationPipeline:
        registry = EngineRegistry()
        registry.register(IndexTranslateEngine(base_url=self.base, timeout=10))
        pipeline = TranslationPipeline(
            registry=registry,
            rate_limiter=rate,
            fallback_chain=chain,
            cache_db=cache,
        )
        pipeline.cache_enabled = cache is not None
        return pipeline

    def request(self, model: str = "Index-Translate-35B-A3B") -> TranslateRequest:
        return TranslateRequest(
            text="你好",
            target_lang="en",
            model=model,
            extra={"api_base": self.base, "timeout": 10},
        )


class PromptParityAtEngineLevelTests(unittest.TestCase):
    """The refactor must not move a single byte of the prompt."""

    def test_text_prompt_matches_call_api(self) -> None:
        engine = IndexTranslateEngine()
        req = TranslateRequest(
            text="技术赋能创作，让交流跨越语言的界限。",
            target_lang="en",
            source_lang="auto",
            instruction="保持简洁",
        )
        content, max_tokens = engine._content(req, ["API→接口", "限流→rate limit"])
        expected = call_api.build_prompt(
            req.text,
            "en",
            "auto",
            instruction="保持简洁",
            glossary="API→接口、限流→rate limit",
        )
        self.assertEqual(content, expected)
        self.assertEqual(max_tokens, min(4096, max(1024, int(len(req.text) * 1.25) + 256)))

    def test_image_prompt_maps_request_fields(self) -> None:
        """The image branch must carry ref_text / instruction into the V1 prompt."""
        engine = IndexTranslateEngine()
        req = TranslateRequest(
            text="参考：营业时间",
            image_b64="data:image/png;base64,AAAA",
            target_lang="zh",
            source_lang="en",
            instruction="保留数字",
        )
        content, max_tokens = engine._content(req, [])

        self.assertEqual(content[0]["type"], "text")
        self.assertIn("【参考文本/关注内容】\n参考：营业时间", content[0]["text"])
        self.assertIn("【指令要求】保留数字", content[0]["text"])
        self.assertTrue(content[0]["text"].endswith("只输出翻译结果，不要进行任何解释。"))
        self.assertEqual(
            content[1],
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        )
        self.assertEqual(max_tokens, 2048)


class FallbackTests(_PipelineBase):
    mode = "429_then_ok"

    def test_falls_back_to_next_model_before_first_token(self) -> None:
        pipeline = self.make_pipeline(chain=[("index_translate", "Index-Translate-9B")])
        chunks = list(pipeline.execute_stream(self.request()))

        text = "".join(chunk.delta for chunk in chunks)
        self.assertEqual(text, "Hello world")
        self.assertFalse(any(chunk.from_cache for chunk in chunks))
        # The 35B call was refused, the answer came from the 9B model.
        self.assertEqual(self.handler.calls[0], "Index-Translate-35B-A3B")
        self.assertEqual(chunks[-1].model_used, "Index-Translate-9B")
        self.assertEqual(chunks[-1].engine_used, "index_translate")
        self.assertTrue(chunks[-1].done)


class MidStreamErrorTests(_PipelineBase):
    mode = "midstream"

    def test_does_not_splice_a_second_model(self) -> None:
        pipeline = self.make_pipeline(chain=[("index_translate", "Index-Translate-9B")])
        seen: list[str] = []
        with self.assertRaises(RetryableEngineError):
            for chunk in pipeline.execute_stream(self.request()):
                if chunk.delta:
                    seen.append(chunk.delta)

        self.assertEqual("".join(seen), "Half a sen")
        # Exactly one upstream attempt: no silent 9B retry after partial output.
        self.assertEqual(len(self.handler.calls), 1)


class CacheTests(_PipelineBase):
    def test_second_identical_request_is_served_from_cache(self) -> None:
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        cache = TranslationCacheDB(Path(tmp.name) / "c.db")
        self.addCleanup(cache.close)

        pipeline = self.make_pipeline(cache=cache)
        first = list(pipeline.execute_stream(self.request()))
        second = list(pipeline.execute_stream(self.request()))

        self.assertEqual("".join(c.delta for c in first), "Hello world")
        self.assertEqual(len(self.handler.calls), 1)
        self.assertEqual(len(second), 1)
        self.assertTrue(second[0].from_cache)
        self.assertEqual(second[0].delta, "Hello world")


class TermMatchingTests(unittest.TestCase):
    def test_ascii_terms_need_word_boundaries(self) -> None:
        pairs = [("cat", "猫"), ("API", "接口"), ("量子", "Quantum")]
        hits = [term for term, _ in term_hits("category uses API, 量子计算", pairs)]
        self.assertEqual(hits, ["API", "量子"])
        self.assertNotIn("cat", hits)

    def test_longest_term_wins_order(self) -> None:
        pairs = [("API", "接口"), ("API 网关", "API Gateway")]
        hits = term_hits("请配置 API 网关", pairs)
        self.assertEqual([term for term, _ in hits], ["API 网关", "API"])

    def test_case_insensitive(self) -> None:
        self.assertEqual(term_hits("click Rate Limit now", [("rate limit", "限流")]),
                         [("rate limit", "限流")])


class RateLimitTests(_PipelineBase):
    delay = 0.25

    def test_concurrency_never_exceeds_the_limit(self) -> None:
        limiter = RateLimiter(max_concurrency=2, min_interval_sec=0.0)
        pipeline = self.make_pipeline(rate=limiter)

        results: list[str] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append("".join(c.delta for c in pipeline.execute_stream(self.request())))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 5)
        self.assertLessEqual(self.handler.peak, 2)
        self.assertEqual(limiter.stats()["served"], 5)

    def test_cancel_while_queued_returns_immediately(self) -> None:
        limiter = RateLimiter(max_concurrency=1, min_interval_sec=0.0)
        pipeline = self.make_pipeline(rate=limiter)

        holder = threading.Event()
        release = threading.Event()

        def occupy() -> None:
            with limiter.acquire() as admitted:
                holder.set()
                release.wait(5)

        blocker = threading.Thread(target=occupy, daemon=True)
        blocker.start()
        self.assertTrue(holder.wait(5))

        cancel = threading.Event()
        timer = threading.Timer(0.05, cancel.set)
        timer.start()

        started = time.monotonic()
        chunks = list(pipeline.execute_stream(self.request(), cancel))
        elapsed = time.monotonic() - started
        release.set()
        blocker.join(5)

        self.assertEqual(chunks, [])
        self.assertLess(elapsed, 0.8)


if __name__ == "__main__":
    unittest.main()
