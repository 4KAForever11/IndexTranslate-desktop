"""设备名清洗：中文设备名不能被解成「◆」。"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "extensions_src" / "ext-live-asr" / "main.py"

_spec = importlib.util.spec_from_file_location("ext_asr_main", MODULE_PATH)
asr_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asr_main)

POOL = [
    "耳机 (HyperX Cloud Alpha S Game)",
    "扬声器 (Realtek(R) Audio)",
    "显示器 (NVIDIA High Definition Audio)",
]


class DeviceNameTests(unittest.TestCase):
    def test_strips_replacement_characters(self) -> None:
        raw = "\ufffd\ufffd\ufffd (HyperX Cloud Alpha S Game) [Loopback]"
        cleaned = asr_main._clean_name(raw)
        self.assertNotIn("\ufffd", cleaned)
        self.assertIn("HyperX Cloud Alpha S Game", cleaned)

    def test_repairs_from_registry_pool(self) -> None:
        raw = "\ufffd\ufffd\ufffd (HyperX Cloud Alpha S Game) [Loopback]"
        repaired = asr_main._repair_from_registry(raw, POOL)
        self.assertTrue(repaired.startswith("耳机"), repaired)
        self.assertTrue(repaired.endswith("[Loopback]"), repaired)

    def test_repair_keeps_unknown_names(self) -> None:
        raw = "\ufffd (Unknown Thing) [Loopback]"
        self.assertEqual(asr_main._repair_from_registry(raw, POOL), raw)

    def test_repair_ignores_clean_names(self) -> None:
        clean = "扬声器 (Realtek(R) Audio) [Loopback]"
        self.assertEqual(asr_main._repair_from_registry(clean, POOL), clean)

    def test_decodes_gbk_bytes(self) -> None:
        self.assertEqual(asr_main._clean_name("扬声器".encode("gbk")), "扬声器")

    def test_decodes_utf8_bytes(self) -> None:
        self.assertEqual(asr_main._clean_name("耳机".encode("utf-8")), "耳机")

    def test_empty_name(self) -> None:
        self.assertEqual(asr_main._clean_name(""), "")
        self.assertEqual(asr_main._clean_name(None), "")

    def test_collapses_whitespace(self) -> None:
        self.assertEqual(asr_main._clean_name("  Monitor   (A)\n[Loopback] "), "Monitor (A) [Loopback]")


if __name__ == "__main__":
    unittest.main()
