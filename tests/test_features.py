"""Tests for the style presets, glossary groups and batch queue."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.core import batch
from app.core.glossary_db import DEFAULT_GROUP, GlossaryDB
from app.core.styles import (
    DEFAULT_STYLES,
    MAX_STYLES,
    default_styles,
    find_style,
    load_styles,
    new_style_id,
    style_text,
)


class StylePresetTests(unittest.TestCase):
    def test_builtin_defaults(self):
        styles = default_styles()
        ids = [item["id"] for item in styles]
        self.assertEqual(ids, ["plain", "natural", "tech", "academic", "acg"])
        # The first preset must stay empty so prompt parity is preserved.
        self.assertEqual(style_text(styles, "plain"), "")
        self.assertTrue(all(item["name"] for item in styles))
        self.assertTrue(all(item["text"] for item in styles[1:]))

    def test_load_styles_falls_back_to_builtins(self):
        for raw in (None, [], "nonsense", [{"id": ""}], [{}]):
            styles = load_styles(raw)
            self.assertEqual(
                [item["id"] for item in styles], [item["id"] for item in DEFAULT_STYLES]
            )

    def test_load_styles_keeps_custom_and_dedupes(self):
        raw = [
            {"id": "mine", "name": "我的风格", "text": "用一句话总结"},
            {"id": "mine", "name": "重复 id", "text": "忽略我"},
            {"name": "", "text": "没有名字的条目"},
        ]
        styles = load_styles(raw)
        self.assertEqual(len(styles), 1)
        self.assertEqual(styles[0]["id"], "mine")
        self.assertEqual(styles[0]["text"], "用一句话总结")

    def test_load_styles_assigns_ids_and_caps_count(self):
        raw = [{"name": f"风格{i}", "text": "t"} for i in range(60)]
        styles = load_styles(raw)
        self.assertEqual(len(styles), MAX_STYLES)
        self.assertTrue(all(style["id"] for style in styles))
        self.assertEqual(len({style["id"] for style in styles}), MAX_STYLES)

    def test_find_and_unknown(self):
        styles = default_styles()
        self.assertIsNotNone(find_style(styles, "tech"))
        self.assertIsNone(find_style(styles, "nope"))
        self.assertEqual(style_text(styles, "nope"), "")
        self.assertNotIn(new_style_id(styles), [item["id"] for item in styles])


class GlossaryGroupTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._dir.cleanup)
        self.db = GlossaryDB(Path(self._dir.name) / "glossary.db")
        self.addCleanup(self.db.close)

    def test_default_group_exists_and_is_enabled(self):
        self.assertEqual(self.db.group_names(), [DEFAULT_GROUP])
        self.assertEqual(self.db.enabled_groups(), [DEFAULT_GROUP])

    def test_same_term_two_groups(self):
        self.db.upsert("rush", "冲锋", group="游戏")
        self.db.upsert("draft", "草稿", group="开发")
        self.db.upsert("draft", "选秀", group="体育")
        self.assertEqual(self.db.get("draft", group="开发"), "草稿")
        self.assertEqual(self.db.get("draft", group="体育"), "选秀")
        self.assertEqual(self.db.count(group="开发"), 1)
        self.assertEqual(self.db.count(), 3)

    def test_match_only_injects_present_terms(self):
        self.db.upsert("API", "接口")
        self.db.upsert("API 网关", "API Gateway")
        self.db.upsert("未出现的术语", "never")
        pairs = self.db.match_pairs("请翻译 API 网关的说明。")
        self.assertEqual(pairs, [("API 网关", "API Gateway"), ("API", "接口")])
        self.assertEqual(
            self.db.prompt_for("请翻译 API 网关的说明。"), "API 网关→API Gateway、API→接口"
        )
        self.assertEqual(self.db.prompt_for("无关文本"), "")

    def test_match_respects_enabled_groups(self):
        self.db.upsert("季卡", "Season Pass", group="游戏")
        self.assertIn("季卡", self.db.prompt_string())
        self.db.set_group_enabled("游戏", False)
        self.assertNotIn("季卡", self.db.prompt_for("买季卡"))
        self.db.set_group_enabled("游戏", True)
        self.assertIn("季卡", self.db.prompt_for("买季卡"))

    def test_match_caps_terms(self):
        for index in range(50):
            self.db.upsert(f"术语{index}", f"t{index}")
        text = "".join(f"术语{index} " for index in range(50))
        self.assertLessEqual(len(self.db.match_pairs(text)), 40)

    def test_rename_and_delete_group(self):
        self.db.upsert("a", "A", group="旧组")
        self.db.rename_group("旧组", "新组")
        self.assertIn("新组", self.db.group_names())
        self.assertNotIn("旧组", self.db.group_names())
        self.assertEqual(self.db.get("a", group="新组"), "A")
        self.db.delete_group("新组")
        self.assertNotIn("新组", self.db.group_names())
        self.assertEqual(self.db.count(), 0)

    def test_migrates_legacy_schema(self):
        path = Path(self._dir.name) / "legacy.db"
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE terms (term TEXT PRIMARY KEY COLLATE NOCASE, "
            "translation TEXT, created_at TEXT DEFAULT '')"
        )
        conn.execute("INSERT INTO terms (term, translation) VALUES ('光遇', 'Sky')")
        conn.commit()
        conn.close()

        db = GlossaryDB(path)
        self.addCleanup(db.close)
        self.assertEqual(db.get("光遇"), "Sky")
        self.assertEqual(db.group_names(), [DEFAULT_GROUP])
        self.assertEqual(db.prompt_for("光遇在哪里下载"), "光遇→Sky")


class BatchSegmentTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def _finish(self, item, translations):
        for index, text in enumerate(translations):
            item.results[index] = text
        item.status = batch.STATUS_DONE
        return item

    def test_supported_and_kind(self):
        self.assertTrue(batch.supported("a.PNG"))
        self.assertTrue(batch.supported("a.srt"))
        self.assertTrue(batch.supported("a.md"))
        self.assertFalse(batch.supported("a.exe"))
        self.assertEqual(batch.item_kind("a.png"), batch.KIND_IMAGE)
        self.assertEqual(batch.item_kind("a.srt"), batch.KIND_TEXT)

    def test_markdown_paragraphs(self):
        path = self.root / "notes.md"
        long_a = (
            "第一段说的是批量翻译会按段落切分，每一段单独请求，"
            "失败的那段还可以单独重试，所以整份文档不会因为一条报错而白等。"
        )
        long_b = (
            "- 列表项一：把整个文件夹拖进来就能自动排队，重复文件会自动跳过\n"
            "- 列表项二：全部完成后合并导出成一份双语 Markdown"
        )
        long_c = (
            "| 文件 | 状态 | 用时 |\n| - | - | - |\n| notes.md | 完成 | 2.4s |"
        )
        path.write_text(f"{long_a}\n\n{long_b}\n\n{long_c}\n", encoding="utf-8")
        item = batch.build_item(path)
        self.assertEqual(item.kind, batch.KIND_TEXT)
        self.assertEqual(len(item.segments), 3)
        self.assertIn("列表项一", item.segments[1].text)
        self.assertIn("| notes.md | 完成 |", item.segments[2].text)
        self.assertEqual(len(item.results), 3)

    def test_short_paragraphs_merge(self):
        path = self.root / "short.md"
        path.write_text("第一句。\n\n第二句。\n\n第三句。\n", encoding="utf-8")
        item = batch.build_item(path)
        self.assertEqual(len(item.segments), 1)
        self.assertIn("第三句", item.segments[0].text)

    def test_srt_parsing(self):
        path = self.root / "subs.srt"
        path.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nHello there\nsecond line\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\nBye\n",
            encoding="utf-8",
        )
        item = batch.build_item(path)
        self.assertEqual(len(item.segments), 2)
        self.assertEqual(item.segments[0].text, "Hello there\nsecond line")
        self.assertIn("00:00:01,000", item.segments[0].label)
        self.assertEqual(item.segments[1].text, "Bye")

    def test_long_text_is_split(self):
        path = self.root / "long.txt"
        path.write_text("句子。" * 8000, encoding="utf-8")
        item = batch.build_item(path)
        self.assertGreater(len(item.segments), 1)
        self.assertTrue(all(len(seg.text) <= batch.MAX_INPUT_CHARS for seg in item.segments))

    def test_export_modes(self):
        path = self.root / "doc.md"
        path.write_text("Hello world.\n", encoding="utf-8")
        item = self._finish(batch.build_item(path), ["你好，世界。"])

        bilingual = batch.export_markdown([item], "bilingual")
        self.assertIn("Hello world.", bilingual)
        self.assertIn("你好，世界。", bilingual)

        target = batch.export_markdown([item], "target")
        self.assertNotIn("Hello world.", target)
        self.assertIn("你好，世界。", target)

        both = batch.export_markdown([item], "both")
        self.assertIn("**原文**", both)
        self.assertIn("**译文**", both)
        self.assertIn("Hello world.", both)
        self.assertIn("你好，世界。", both)

    def test_unfinished_items_are_skipped(self):
        path = self.root / "doc.md"
        path.write_text("Hello world.\n", encoding="utf-8")
        item = batch.build_item(path)
        self.assertEqual(batch.export_markdown([item], "bilingual"), "")

    def test_image_item_placeholder(self):
        from PIL import Image

        path = self.root / "shot.png"
        Image.new("RGB", (64, 32), "white").save(path)
        item = self._finish(batch.build_item(path), ["译文"])
        self.assertEqual(item.kind, batch.KIND_IMAGE)
        self.assertEqual(len(item.segments), 1)
        markdown = batch.export_markdown([item], "bilingual")
        self.assertIn("（图片：shot.png）", markdown)
        self.assertIn("译文", markdown)

    def test_collect_paths_expands_directory(self):
        (self.root / "one.png").write_bytes(b"x")
        (self.root / "two.md").write_text("hi", encoding="utf-8")
        (self.root / "skip.exe").write_bytes(b"x")
        found = batch.collect_paths([self.root])
        self.assertEqual(sorted(path.name for path in found), ["one.png", "two.md"])

    def test_collect_paths_dedupes(self):
        path = self.root / "one.md"
        path.write_text("hi", encoding="utf-8")
        found = batch.collect_paths([path, path, self.root])
        self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()