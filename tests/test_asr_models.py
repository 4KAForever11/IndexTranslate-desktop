"""识别模型的路径、状态文案与配置写回。

这些逻辑决定了扩展中心那张卡片上写什么、字幕能不能启动，
所以不依赖网络、只用临时目录把边界情况钉住。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import asr_models


class _FakeSettings:
    """只实现 asr_models 用到的那三个方法。"""

    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.saved = 0

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        self.saved += 1


def _make_model(folder: Path, *, model: str = "model.int8.onnx", tokens: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / model).write_bytes(b"\0" * 1024)
    if tokens:
        (folder / "tokens.txt").write_text("a 0\nb 1\n", encoding="utf-8")
    return folder


class PathLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_models_root_and_target_dir(self):
        self.assertEqual(asr_models.models_root(self.base), self.base / "models")
        self.assertEqual(
            asr_models.target_dir(self.base), self.base / "models" / asr_models.MODEL_ID
        )

    def test_total_bytes_matches_the_two_files(self):
        self.assertEqual(asr_models.total_bytes(), 239_233_841 + 315_894)
        self.assertGreater(asr_models.total_bytes(), 200 * 1048576)

    def test_first_mirror_is_the_cn_friendly_one(self):
        self.assertTrue(asr_models.MIRRORS[0].startswith("https://hf-mirror.com/"))
        self.assertTrue(asr_models.MIRRORS[-1].startswith("https://huggingface.co/"))


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ready_with_int8_and_tokens(self):
        folder = _make_model(self.base / "m")
        self.assertTrue(asr_models.is_ready(folder))
        self.assertEqual(Path(asr_models.model_file(folder)).name, "model.int8.onnx")

    def test_ready_with_fp32_model(self):
        folder = _make_model(self.base / "m", model="model.onnx")
        self.assertTrue(asr_models.is_ready(folder))

    def test_missing_tokens_is_not_ready(self):
        folder = _make_model(self.base / "m", tokens=False)
        self.assertFalse(asr_models.is_ready(folder))
        self.assertIn("tokens.txt", asr_models.describe(folder)[0])

    def test_empty_model_file_is_ignored(self):
        folder = self.base / "m"
        folder.mkdir()
        (folder / "model.int8.onnx").write_bytes(b"")
        (folder / "tokens.txt").write_text("a 0\n", encoding="utf-8")
        self.assertFalse(asr_models.is_ready(folder))

    def test_describe_missing_directory(self):
        text, ready = asr_models.describe(self.base / "nope")
        self.assertFalse(ready)
        self.assertIn("未下载", text)

    def test_describe_partial_download(self):
        folder = self.base / "m"
        folder.mkdir()
        (folder / "model.int8.onnx.part").write_bytes(b"\0" * 10)
        text, ready = asr_models.describe(folder)
        self.assertFalse(ready)
        self.assertIn("下载未完成", text)

    def test_describe_ready_reports_size(self):
        folder = _make_model(self.base / "m")
        text, ready = asr_models.describe(folder)
        self.assertTrue(ready)
        self.assertIn("已就绪", text)
        self.assertIn(str(folder), text)


class ConfiguredDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_configured_directory_wins(self):
        folder = _make_model(self.base / "custom")
        fake = _FakeSettings({"live_asr_model": str(folder)})
        with mock.patch.object(asr_models, "settings", fake):
            self.assertEqual(asr_models.configured_dir(), str(folder))

    def test_stale_setting_falls_back_to_default_dir(self):
        ready = _make_model(asr_models.target_dir(self.home))
        fake = _FakeSettings({"live_asr_model": str(self.base / "gone")})
        with mock.patch.object(asr_models, "settings", fake), mock.patch.object(
            asr_models, "data_dir", lambda: str(self.home)
        ):
            self.assertEqual(asr_models.configured_dir(), str(ready))

    def test_nothing_ready_returns_empty(self):
        fake = _FakeSettings()
        with mock.patch.object(asr_models, "settings", fake), mock.patch.object(
            asr_models, "data_dir", lambda: str(self.home)
        ):
            self.assertEqual(asr_models.configured_dir(), "")

    def test_ensure_configured_persists_path(self):
        folder = _make_model(self.base / "custom")
        fake = _FakeSettings()
        with mock.patch.object(asr_models, "settings", fake):
            self.assertEqual(asr_models.ensure_configured(folder), str(folder))
        self.assertEqual(fake.data["live_asr_model"], str(folder))
        self.assertEqual(fake.saved, 1)

    def test_ensure_configured_rejects_incomplete_dir(self):
        folder = _make_model(self.base / "custom", tokens=False)
        fake = _FakeSettings()
        with mock.patch.object(asr_models, "settings", fake):
            with self.assertRaises(asr_models.AsrModelError):
                asr_models.ensure_configured(folder)
        self.assertNotIn("live_asr_model", fake.data)


if __name__ == "__main__":
    unittest.main()
