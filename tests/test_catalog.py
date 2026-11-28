"""扩展在线目录的解析与校验测试（不联网）。"""

from __future__ import annotations

import json
import unittest

from app.core.extensions.catalog import (
    CatalogError,
    CatalogEntry,
    entry_for,
    parse_catalog,
)


class CatalogParseTests(unittest.TestCase):
    def test_parses_object_form(self) -> None:
        raw = json.dumps(
            {
                "version": 1,
                "extensions": [
                    {
                        "id": "ext-ocr-overlay",
                        "name": "本地 OCR 原位贴片",
                        "version": "1.1.0",
                        "url": "https://example.com/ext-ocr-overlay-1.1.0.zip",
                        "sha256": "A" * 64,
                        "size": 5 * 1024 * 1024,
                        "host_min": "2.0.0",
                        "description": "本地识别",
                        "deps": ["rapidocr-onnxruntime"],
                    }
                ],
            }
        )
        entries = parse_catalog(raw)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.id, "ext-ocr-overlay")
        self.assertEqual(entry.version, "1.1.0")
        self.assertEqual(entry.sha256, "a" * 64, "sha256 应统一成小写")
        self.assertEqual(entry.size_text, "5.0 MB")
        self.assertEqual(entry.deps, ("rapidocr-onnxruntime",))
        self.assertTrue(entry.host_ok("2.1.0"))
        self.assertFalse(entry.host_ok("1.9.9"))

    def test_parses_bare_list(self) -> None:
        entries = parse_catalog(
            [
                {
                    "id": "ext-a",
                    "name": "A",
                    "version": "1.0.0",
                    "url": "https://example.com/a.zip",
                }
            ]
        )
        self.assertEqual([entry.id for entry in entries], ["ext-a"])
        self.assertEqual(entries[0].sha256, "")

    def test_accepts_bytes(self) -> None:
        raw = json.dumps({"extensions": []}).encode("utf-8")
        self.assertEqual(parse_catalog(raw), [])

    def test_rejects_newer_catalog_version(self) -> None:
        with self.assertRaises(CatalogError) as ctx:
            parse_catalog(json.dumps({"version": 99, "extensions": []}))
        self.assertIn("升级", str(ctx.exception))

    def test_rejects_bad_json(self) -> None:
        with self.assertRaises(CatalogError) as ctx:
            parse_catalog("{not json")
        self.assertIn("JSON", str(ctx.exception))

    def test_rejects_missing_id(self) -> None:
        with self.assertRaises(CatalogError) as ctx:
            parse_catalog([{"name": "x", "url": "https://example.com/x.zip"}])
        self.assertIn("id", str(ctx.exception))

    def test_rejects_non_http_url(self) -> None:
        with self.assertRaises(CatalogError) as ctx:
            parse_catalog([{"id": "ext-a", "url": "file:///tmp/x.zip"}])
        self.assertIn("http", str(ctx.exception))

    def test_dedupes_same_id(self) -> None:
        entries = parse_catalog(
            [
                {"id": "ext-a", "version": "1.0.0", "url": "https://e.com/1.zip"},
                {"id": "ext-a", "version": "2.0.0", "url": "https://e.com/2.zip"},
            ]
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].version, "1.0.0", "先出现的条目优先")

    def test_skips_non_dict_items(self) -> None:
        entries = parse_catalog([{"id": "ext-a", "url": "https://e.com/a.zip"}, "junk", 3])
        self.assertEqual([entry.id for entry in entries], ["ext-a"])

    def test_size_text(self) -> None:
        self.assertEqual(CatalogEntry(id="a", name="a", version="1", url="u", size=0).size_text, "未知大小")
        self.assertEqual(CatalogEntry(id="a", name="a", version="1", url="u", size=2048).size_text, "2 KB")

    def test_entry_for(self) -> None:
        entries = parse_catalog([{"id": "ext-a", "url": "https://e.com/a.zip"}])
        self.assertIsNotNone(entry_for(entries, "ext-a"))
        self.assertIsNone(entry_for(entries, "ext-b"))


if __name__ == "__main__":
    unittest.main()
