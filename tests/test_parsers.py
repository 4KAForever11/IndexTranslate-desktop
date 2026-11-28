"""解析器与占位符保护的单元测试（不依赖网络与 Qt）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core.parsers import (
    Guard,
    JsonI18nParser,
    MarkdownParser,
    PlaceholderGuard,
    SubtitleParser,
    group_paths,
    natural_key,
    parser_for,
    supported_extensions,
)
from app.core.parsers.base import STATUS_DONE, STATUS_SKIPPED, TranslationUnit
from app.core.parsers.image_batch_parser import ImageBatchParser


def _mark_done(unit: TranslationUnit, text: str | None = None) -> TranslationUnit:
    unit.status = STATUS_DONE
    unit.translated_text = text if text is not None else f"[译]{unit.source_text}"
    return unit


class PlaceholderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = PlaceholderGuard()

    def test_protects_backticks_html_braces_and_percent(self) -> None:
        text = "执行 `npm run build` 后写入 {count} 条，模板 {{ name }}，格式 %s。"
        protected, mapping = self.guard.protect(text)
        self.assertEqual(len(mapping), 4)
        for token in mapping:
            self.assertIn(token, protected)
            self.assertNotIn("`npm run build`", protected)
        restored, missing = self.guard.restore(protected, mapping)
        self.assertEqual(restored, text)
        self.assertEqual(missing, [])

    def test_reports_missing_tokens(self) -> None:
        _protected, mapping = self.guard.protect("`a` 与 {b}")
        broken = "只保留 ⟦P0⟧ 的译文"
        restored, missing = self.guard.restore(broken, mapping)
        self.assertEqual(restored, "只保留 `a` 的译文")
        self.assertEqual(len(missing), 1)
        self.assertNotIn("⟦P1⟧", restored)

    def test_strips_invented_tokens(self) -> None:
        protected, mapping = self.guard.protect("`cmd`")
        text = protected + " 多余 ⟦P7⟧"
        restored, missing = self.guard.restore(text, mapping)
        self.assertNotIn("⟦P7⟧", restored)
        self.assertEqual(missing, [])

    def test_hint_only_when_mapping(self) -> None:
        self.assertEqual(Guard.hint_for({}), "")
        _protected, mapping = self.guard.protect("`x` 和 {y}")
        hint = Guard.hint_for(mapping)
        self.assertIn("⟦P0⟧", hint)
        self.assertIn("⟦P1⟧", hint)


class MarkdownTests(unittest.TestCase):
    DOC = """---
title: demo
---

第一段内容，需要翻译成英文。

```python
print("hello")
```

第二段内容，也有一段 `inline code` 和 {count} 占位符。
"""

    def test_parse_marks_frontmatter_and_fence_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "a.md"
            path.write_text(self.DOC, encoding="utf-8")
            units = MarkdownParser().parse([path])
        skipped = [u for u in units if u.status == STATUS_SKIPPED]
        translated = [u for u in units if u.status != STATUS_SKIPPED]
        self.assertEqual(len(skipped), 2)  # frontmatter + fenced code
        self.assertGreaterEqual(len(translated), 2)
        self.assertTrue(any("print" in u.source_text for u in skipped))
        self.assertTrue(any("{count}" not in u.source_text for u in translated))

    def test_bilingual_export_keeps_code_and_adds_translation(self) -> None:
        parser = MarkdownParser()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "a.md"
            path.write_text(self.DOC, encoding="utf-8")
            units = [u if u.status == STATUS_SKIPPED else _mark_done(u) for u in parser.parse([path])]
            out = parser.export([path], units, Path(tmp) / "out.md", "bilingual")
            text = out.read_text(encoding="utf-8")
        self.assertIn('print("hello")', text)
        self.assertIn("title: demo", text)
        self.assertIn("[译]第一段内容", text)
        self.assertIn("第一段内容", text)  # 原文仍在

    def test_monolingual_export_drops_original_paragraphs(self) -> None:
        parser = MarkdownParser()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "a.md"
            path.write_text(self.DOC, encoding="utf-8")
            units = [u if u.status == STATUS_SKIPPED else _mark_done(u) for u in parser.parse([path])]
            out = parser.export([path], units, Path(tmp) / "out.md", "monolingual")
            text = out.read_text(encoding="utf-8")
        self.assertIn("[译]第一段内容", text)
        self.assertNotIn("\n第一段内容，需要翻译成英文。\n", text)

    def test_long_block_is_split_and_rejoinable(self) -> None:
        paragraph = "这是一个很长的段落。" * 2000
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "long.md"
            path.write_text(paragraph, encoding="utf-8")
            units = MarkdownParser().parse([path])
        self.assertGreater(len(units), 1)
        for unit in units:
            self.assertLessEqual(len(unit.meta.get("protected") or ""), 12000)
        joined = "".join(u.source_text for u in units)
        self.assertEqual(joined, paragraph)


class SubtitleTests(unittest.TestCase):
    SRT = """1
00:00:01,000 --> 00:00:03,000
Hello there,
second line

2
00:00:04,200 --> 00:00:06,800
This is a test.

3
00:01:02,300 --> 00:01:05,100
Goodbye
"""

    def test_parse_units_and_timings(self) -> None:
        parser = SubtitleParser()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "a.srt"
            path.write_text(self.SRT, encoding="utf-8")
            units = parser.parse([path])
        self.assertEqual(len(units), 3)
        self.assertEqual(units[0].meta["timing"], "00:00:01,000 --> 00:00:03,000")
        self.assertIn("second line", units[0].source_text)
        self.assertEqual(units[2].meta["index_text"], "3")

    def test_export_keeps_timeline_and_numbering_byte_exact(self) -> None:
        parser = SubtitleParser()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "a.srt"
            path.write_text(self.SRT, encoding="utf-8")
            units = [_mark_done(u) for u in parser.parse([path])]
            out = parser.export([path], units, Path(tmp) / "out.srt", "bilingual")
            text = out.read_text(encoding="utf-8")
        for timing in ("00:00:01,000 --> 00:00:03,000", "00:01:02,300 --> 00:01:05,100"):
            self.assertIn(timing, text)
        blocks = [b for b in text.split("\n\n") if b.strip()]
        self.assertEqual(blocks[0].splitlines()[0], "1")
        self.assertEqual(blocks[2].splitlines()[0], "3")
        first = blocks[0].splitlines()
        self.assertEqual(first[2], "[译]Hello there,")
        self.assertEqual(first[3], "second line")
        self.assertEqual(first[4], "Hello there,")

    def test_monolingual_export_replaces_text(self) -> None:
        parser = SubtitleParser()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "a.srt"
            path.write_text(self.SRT, encoding="utf-8")
            units = [_mark_done(u) for u in parser.parse([path])]
            out = parser.export([path], units, Path(tmp) / "out.srt", "monolingual")
            text = out.read_text(encoding="utf-8")
        self.assertIn("[译]This is a test.", text)
        self.assertNotIn("\nThis is a test.\n", text)

    def test_vtt_header_is_preserved(self) -> None:
        parser = SubtitleParser()
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nHello\n"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "a.vtt"
            path.write_text(vtt, encoding="utf-8")
            units = parser.parse([path])
            cues = [u for u in units if u.status != STATUS_SKIPPED]
            self.assertEqual(len(cues), 1)
            cue = _mark_done(cues[0])
            out = parser.export([path], [u for u in units if u.status == STATUS_SKIPPED] + [cue], Path(tmp) / "out.vtt", "bilingual")
            text = out.read_text(encoding="utf-8")
        self.assertIn("WEBVTT", text)
        self.assertIn("00:00:01.000 --> 00:00:03.000", text)


class JsonTests(unittest.TestCase):
    DATA = {
        "menu": {"file": "File", "open": "Open file"},
        "count": 3,
        "active": True,
        "homepage": "https://example.com",
        "items": ["First item", "Second item"],
        "nested": {"deep": {"label": "Deep label"}},
    }

    def test_skips_numbers_urls_and_booleans(self) -> None:
        parser = JsonI18nParser()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "zh.json"
            path.write_text(json.dumps(self.DATA, ensure_ascii=False, indent=2), encoding="utf-8")
            units = parser.parse([path])
        pointers = {u.meta["pointer"] for u in units}
        self.assertEqual(
            pointers,
            {"menu.file", "menu.open", "items[0]", "items[1]", "nested.deep.label"},
        )

    def test_export_is_valid_json_with_preserved_indent(self) -> None:
        parser = JsonI18nParser()
        raw = json.dumps(self.DATA, ensure_ascii=False, indent=4)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "zh.json"
            path.write_text(raw, encoding="utf-8")
            units = [_mark_done(u) for u in parser.parse([path])]
            out = parser.export([path], units, Path(tmp) / "en.json", "monolingual")
            text = out.read_text(encoding="utf-8")
            parsed = json.loads(text)
            extras = list(parser.last_extras)
        self.assertEqual(parsed["menu"]["file"], "[译]File")
        self.assertEqual(parsed["count"], 3)
        self.assertEqual(parsed["homepage"], "https://example.com")
        self.assertTrue(text.splitlines()[1].startswith("    "))  # 4 空格缩进保留
        self.assertEqual(extras, [])

    def test_bilingual_writes_comparison_table(self) -> None:
        parser = JsonI18nParser()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "zh.json"
            path.write_text(json.dumps(self.DATA, ensure_ascii=False, indent=2), encoding="utf-8")
            units = [_mark_done(u) for u in parser.parse([path])]
            out = parser.export([path], units, Path(tmp) / "en.json", "bilingual")
            table = Path(tmp) / "en.对照表.md"
            self.assertTrue(table.exists())
            text = table.read_text(encoding="utf-8")
            self.assertEqual(parser.last_extras, [table])
            self.assertIn("menu.file", text)

    def test_placeholder_survives_json_roundtrip(self) -> None:
        parser = JsonI18nParser()
        data = {"tip": "按 {count} 次 `Ctrl+C` 结束"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "zh.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            units = parser.parse([path])
            self.assertEqual(len(units), 1)
            mapping = units[0].meta["mapping"]
            self.assertEqual(len(mapping), 2)
            translated = "press ⟦P0⟧ times ⟦P1⟧ to stop"
            restored, missing = Guard.restore(translated, mapping)
            self.assertEqual(restored, "press {count} times `Ctrl+C` to stop")
            self.assertEqual(missing, [])


class ImageAndRegistryTests(unittest.TestCase):
    def test_natural_sort(self) -> None:
        names = ["page10.png", "page2.png", "Page1.png"]
        ordered = sorted(names, key=natural_key)
        self.assertEqual(ordered, ["Page1.png", "page2.png", "page10.png"])

    def test_image_parse_and_markdown_export(self) -> None:
        parser = ImageBatchParser()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            paths = []
            for i in (10, 2, 1):
                p = Path(tmp) / f"page{i}.png"
                p.write_bytes(b"not-a-real-image")
                paths.append(p)
            units = parser.parse(paths)
            self.assertEqual([u.meta["location"] for u in units], ["page1.png", "page2.png", "page10.png"])
            _mark_done(units[0], "译文一")
            out = parser.export(paths, units, Path(tmp) / "out.md", "bilingual")
            text = out.read_text(encoding="utf-8")
        self.assertIn("# 图片翻译汇总", text)
        self.assertIn("译文一", text)
        self.assertIn("page2.png", text)

    def test_registry_groups_images_together(self) -> None:
        paths = ["a.png", "b.srt", "c.md", "d.jpg"]
        groups = group_paths(paths)
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0][0].parser_id, "image_batch")
        self.assertEqual([p.name for p in groups[0][1]], ["a.png", "d.jpg"])
        self.assertEqual(groups[1][0].parser_id, "subtitle")
        self.assertEqual(groups[2][0].parser_id, "markdown")

    def test_supported_extensions_and_lookup(self) -> None:
        self.assertIn(".srt", supported_extensions())
        self.assertEqual(parser_for("x.json").parser_id, "json_i18n")
        self.assertIsNone(parser_for("x.pdf"))


if __name__ == "__main__":
    unittest.main()
