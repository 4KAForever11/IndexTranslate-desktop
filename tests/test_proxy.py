"""Proxy optimisation unit tests (pure helpers, no network)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.core.proxy_cache import ProxyCache
from app.core.proxy_throttle import RequestGate, TokenBucket
from app.core.proxy_utils import (
    GLOSSARY_HEADER,
    completion_json,
    extract_json_text,
    extract_source_text,
    extract_stream_text,
    inject_glossary,
    make_cache_key,
    match_glossary,
    model_chain,
    sse_from_text,
)


def _payload(text: str = "hello", model: str = "Index-Translate-35B-A3B") -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "temperature": 0.0,
        "stream": False,
    }


class CacheKeyTests(unittest.TestCase):
    def test_same_parameters_produce_same_key(self) -> None:
        self.assertEqual(make_cache_key(_payload()), make_cache_key(_payload()))

    def test_key_ignores_stream_flag(self) -> None:
        """stream 只影响传输方式，不影响译文，所以不能进缓存键。"""
        streaming = dict(_payload(), stream=True)
        self.assertEqual(make_cache_key(_payload()), make_cache_key(streaming))

    def test_key_changes_with_text_model_and_temperature(self) -> None:
        base = make_cache_key(_payload())
        self.assertNotEqual(base, make_cache_key(_payload("其它文本")))
        self.assertNotEqual(base, make_cache_key(_payload(model="Index-Translate-9B")))
        warmer = _payload()
        warmer["temperature"] = 0.7
        self.assertNotEqual(base, make_cache_key(warmer))


class GlossaryTests(unittest.TestCase):
    PAIRS = [("API", "接口"), ("rate limit", "限流"), ("Fluent Design", "流畅设计")]

    def test_match_is_case_insensitive_and_ordered_by_length(self) -> None:
        hits = match_glossary("The api hits a rate limit quickly.", self.PAIRS)
        self.assertEqual(hits, ["rate limit→限流", "API→接口"])

    def test_match_returns_empty_when_nothing_hits(self) -> None:
        self.assertEqual(match_glossary("nothing relevant here", self.PAIRS), [])

    def test_inject_appends_to_last_user_message(self) -> None:
        payload = _payload("请翻译 rate limit 这个词")
        count = inject_glossary(payload, self.PAIRS)
        self.assertEqual(count, 1)
        content = payload["messages"][-1]["content"]
        self.assertTrue(content.startswith("请翻译 rate limit 这个词"))
        self.assertIn(GLOSSARY_HEADER, content)
        self.assertIn("rate limit→限流", content)

    def test_inject_supports_multimodal_content(self) -> None:
        payload = {
            "model": "m",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看看这张图里的 API"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                    ],
                }
            ],
        }
        self.assertEqual(inject_glossary(payload, self.PAIRS), 1)
        parts = payload["messages"][-1]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertIn("API→接口", parts[0]["text"])
        self.assertEqual(parts[-1]["type"], "image_url")

    def test_inject_without_hits_leaves_payload_untouched(self) -> None:
        payload = _payload("完全无关的句子")
        self.assertEqual(inject_glossary(payload, self.PAIRS), 0)
        self.assertEqual(payload["messages"][-1]["content"], "完全无关的句子")

    def test_extract_source_text_prefers_user_turns(self) -> None:
        messages = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user text"},
        ]
        self.assertEqual(extract_source_text(messages), "user text")
        self.assertEqual(extract_source_text([{"role": "system", "content": "only"}]), "only")


class ModelChainTests(unittest.TestCase):
    KNOWN = ["Index-Translate-35B-A3B", "Index-Translate-9B", "Index-Translate-2B"]

    def test_chain_starts_with_primary_then_known_fallbacks(self) -> None:
        chain = model_chain("Index-Translate-35B-A3B", "", self.KNOWN)
        self.assertEqual(chain, ["Index-Translate-35B-A3B", "Index-Translate-9B", "Index-Translate-2B"])

    def test_chain_honours_custom_order_and_dedupes(self) -> None:
        chain = model_chain(
            "Index-Translate-35B-A3B",
            "Index-Translate-2B, Index-Translate-35B-A3B",
            self.KNOWN,
        )
        self.assertEqual(chain, ["Index-Translate-35B-A3B", "Index-Translate-2B"])

    def test_chain_ignores_unknown_models(self) -> None:
        chain = model_chain("Index-Translate-9B", "gpt-4o,Index-Translate-2B", self.KNOWN)
        self.assertEqual(chain, ["Index-Translate-9B", "Index-Translate-2B"])


class FakeResponseTests(unittest.TestCase):
    TEXT = "Technology empowers creation."

    def test_sse_round_trip(self) -> None:
        raw = sse_from_text(self.TEXT, "Index-Translate-35B-A3B")
        self.assertTrue(raw.startswith(b"data: "))
        self.assertTrue(raw.rstrip().endswith(b"[DONE]"))
        self.assertEqual(extract_stream_text(raw), self.TEXT)

    def test_json_payload_round_trip(self) -> None:
        body = json.dumps(completion_json(self.TEXT, "Index-Translate-9B")).encode()
        self.assertEqual(extract_json_text(body), self.TEXT)
        payload = json.loads(body)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], self.TEXT)
        self.assertEqual(payload["model"], "Index-Translate-9B")

    def test_extract_tolerates_garbage(self) -> None:
        self.assertEqual(extract_stream_text(b"not an sse stream"), "")
        self.assertEqual(extract_json_text(b"{oops"), "")


class TokenBucketTests(unittest.TestCase):
    def test_zero_rate_is_unlimited(self) -> None:
        bucket = TokenBucket(0.0, 1)
        started = time.monotonic()
        for _ in range(20):
            self.assertTrue(bucket.acquire(0.5))
        self.assertLess(time.monotonic() - started, 0.2)

    def test_burst_then_throttle(self) -> None:
        bucket = TokenBucket(rate=20.0, burst=2)
        started = time.monotonic()
        for _ in range(4):
            self.assertTrue(bucket.acquire(2.0))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.05)  # 4 个请求 - 2 个突发 = 约 0.1s

    def test_acquire_gives_up_past_timeout(self) -> None:
        bucket = TokenBucket(rate=0.5, burst=1)
        self.assertTrue(bucket.acquire(0.1))
        self.assertFalse(bucket.acquire(0.05))


class RequestGateTests(unittest.TestCase):
    def test_serialises_beyond_max_concurrency(self) -> None:
        gate = RequestGate(max_concurrency=2, qps=0.0, burst=1)
        peak = 0
        active = 0
        with gate.slot(1.0) as first:
            self.assertTrue(first)
            active += 1
            peak = max(peak, active)
        self.assertEqual(peak, 1)
        self.assertEqual(gate.stats()["served"], 1)

    def test_slot_reports_shedding(self) -> None:
        gate = RequestGate(max_concurrency=1, qps=0.0, burst=1, queue_timeout=0.05)
        with gate.slot() as first:
            self.assertTrue(first)
        # 第一个槽位释放后再申请应当成功
        with gate.slot(0.5) as again:
            self.assertTrue(again)
        self.assertGreaterEqual(gate.stats()["served"], 1)


class ProxyCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "cache.db"
        self.cache = ProxyCache(self.path, max_entries=3)
        self.addCleanup(self.cache.close)

    def test_put_get_round_trip(self) -> None:
        self.assertIsNone(self.cache.get("missing"))
        self.cache.put("k", {"text": "你好"}, "m35b")
        self.assertEqual(self.cache.get("k"), {"text": "你好"})
        stats = self.cache.stats()
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["lookups"], 2)

    def test_prune_keeps_newest_entries(self) -> None:
        for index in range(5):
            self.cache.put(f"k{index}", {"text": str(index)}, "m")
        self.assertEqual(self.cache.stats()["entries"], 3)
        self.assertIsNone(self.cache.get("k0"))
        self.assertIsNotNone(self.cache.get("k4"))

    def test_clear_removes_everything(self) -> None:
        self.cache.put("a", {"text": "a"}, "m")
        self.cache.put("b", {"text": "b"}, "m")
        self.assertEqual(self.cache.clear(), 2)
        self.assertEqual(self.cache.stats()["entries"], 0)

    def test_expired_entry_is_dropped(self) -> None:
        self.cache.put("old", {"text": "old"}, "m")
        self.cache._conn.execute("UPDATE responses SET last_used = ?", (time.time() - 8 * 86400,))
        self.cache._conn.commit()
        self.assertIsNone(self.cache.get("old"))

    def test_disabled_cache_returns_nothing(self) -> None:
        self.cache.max_entries = 0
        self.cache.put("k", {"text": "v"}, "m")
        self.assertIsNone(self.cache.get("k"))


if __name__ == "__main__":
    unittest.main()
