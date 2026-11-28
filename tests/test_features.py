"""Tests for the style presets, glossary groups and batch queue."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
