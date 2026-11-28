"""2.3.0 的三处修复：浮窗贴边、字幕语言配对、partial 防抖状态机。

这些用例都不需要真的开窗口：浮窗只测定位数学，字幕只测事件状态机。
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication

from app.ui.live_subtitle_bar import LiveSubtitleController, _asr_language, _lang_pair
from app.ui.popup_window import clamp_into

_APP = QApplication.instance() or QApplication([])


class ClampTests(unittest.TestCase):
    """浮窗永远不能露到屏幕外——这是「长文本看不见」的另一半原因。"""

    AREA = QRect(0, 0, 1920, 1040)

    def test_inside_is_untouched(self) -> None:
        self.assertEqual(clamp_into(self.AREA, 460, 300, 200, 150), (200, 150))

    def test_right_and_bottom_edges(self) -> None:
        x, y = clamp_into(self.AREA, 460, 300, 5000, 5000)
        self.assertEqual(x, 1920 - 460)
        self.assertLessEqual(x + 460, self.AREA.right() + 1)
        self.assertLessEqual(y + 300, self.AREA.bottom() + 1)

    def test_negative_coordinates(self) -> None:
        # 副屏在主屏左边时坐标是负的，也不能被推出去
        self.assertEqual(clamp_into(self.AREA, 460, 300, -500, -500), (0, 0))

    def test_offset_origin_screen(self) -> None:
        area = QRect(1920, 0, 1080, 1040)
        x, y = clamp_into(area, 900, 108, 0, 0)
        self.assertEqual((x, y), (1920, 0))
        x, _ = clamp_into(area, 900, 108, 9999, 10)
        self.assertEqual(x, 1920 + 1080 - 900)

    def test_window_taller_than_screen(self) -> None:
        # 窗口比屏幕还高时优先保证左上角可见，而不是算出一个负数
        x, y = clamp_into(self.AREA, 460, 5000, 100, 100)
        self.assertEqual((x, y), (100, 0))


class LanguageTests(unittest.TestCase):
    def test_auto_is_blank_for_sherpa(self) -> None:
        for value in ("", "auto", "自动检测"):
            self.assertEqual(_asr_language(value), "")

    def test_region_codes_reduced_to_primary(self) -> None:
        self.assertEqual(_asr_language("en"), "en")
        self.assertEqual(_asr_language("zh_hans"), "zh")
        self.assertEqual(_asr_language("fas_arab"), "fas")

    def test_lang_pair_reads_like_settings(self) -> None:
        self.assertIn("→", _lang_pair("en", "zh"))
        self.assertIn("英语", _lang_pair("en", "zh"))
        self.assertIn("自动", _lang_pair("auto", "zh"))


class _StubRuntime:
    def client(self, ext_id: str):  # noqa: ANN001 - 只需要有个 client 方法
        return None


class SubtitleStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = LiveSubtitleController(_StubRuntime())
        self.controller._client = object()  # 假装扩展已经连上

    def test_busy_keeps_latest_sentence(self) -> None:
        # 上一句还在翻时不能把新句子扔掉（旧行为），要留着补翻
        self.controller._busy = True
        self.controller._translate("first")
        self.controller._translate("second")
        self.assertEqual(self.controller._pending_source, "second")

    def test_partial_queues_for_debounce(self) -> None:
        self.controller._on_event({"event": "partial", "data": {"text": "hello there"}})
        self.assertEqual(self.controller._pending_source, "hello there")

    def test_empty_final_does_not_clear_pending(self) -> None:
        self.controller._pending_source = "keep me"
        self.controller._on_event({"event": "final", "data": {"text": ""}})
        self.assertEqual(self.controller._pending_source, "keep me")

    def test_duplicate_text_is_not_translated_twice(self) -> None:
        self.controller._last_translated = "same line"
        self.controller._busy = False
        self.controller._translate("same line")
        self.assertFalse(self.controller._busy)

    def test_languages_follow_extension_settings(self) -> None:
        from app.core import settings

        old_source = settings.get("live_asr_source_lang")
        old_target = settings.get("live_asr_target_lang")
        try:
            settings.set("live_asr_source_lang", "en")
            settings.set("live_asr_target_lang", "zh")
            self.assertEqual(self.controller.source_language(), "en")
            self.assertEqual(self.controller.target_language(), "zh")
            settings.set("live_asr_source_lang", "")
            settings.set("live_asr_target_lang", "")
            self.assertEqual(self.controller.source_language(), str(settings.get("source_lang")))
            self.assertEqual(self.controller.target_language(), str(settings.get("target_lang")))
        finally:
            settings.set("live_asr_source_lang", old_source or "")
            settings.set("live_asr_target_lang", old_target or "")


if __name__ == "__main__":
    unittest.main()
