"""自动更新的选包 / 版本比较 / 安装形态判断（不联网）。"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from app.core.updater import Asset, ReleaseInfo, is_newer, version_key
from app.ui import update_worker


def _release(assets: list[Asset]) -> ReleaseInfo:
    return ReleaseInfo(
        tag="v9.9.9",
        version="9.9.9",
        name="9.9.9",
        notes="",
        page_url="https://example.invalid",
        assets=assets,
    )


FOLDER = Asset(name="IndexTranslate-desktop-9.9.9-win64.zip", url="u1", size=10)
ONEFILE = Asset(name="IndexTranslate-desktop-9.9.9-win64-onefile.exe", url="u2", size=20)
ASR = Asset(name="ext-live-asr-1.2.0.zip", url="u3", size=30)


class VersionTests(unittest.TestCase):
    def test_version_key(self) -> None:
        self.assertEqual(version_key("v2.2.0"), (2, 2, 0))
        self.assertEqual(version_key("2.10.3"), (2, 10, 3))
        self.assertEqual(version_key("nonsense"), (0, 0, 0))

    def test_is_newer(self) -> None:
        self.assertTrue(is_newer("2.10.0", "2.9.9"))
        self.assertFalse(is_newer("2.2.0", "2.2.0"))
        self.assertFalse(is_newer("v2.1.9", "2.2.0"))


class PickAssetTests(unittest.TestCase):
    def test_folder_prefers_zip(self) -> None:
        release = _release([ASR, ONEFILE, FOLDER])
        self.assertIs(update_worker.pick_asset(release, "folder"), FOLDER)

    def test_onefile_prefers_onefile_exe(self) -> None:
        release = _release([ASR, FOLDER, ONEFILE])
        self.assertIs(update_worker.pick_asset(release, "onefile"), ONEFILE)

    def test_falls_back_when_only_one_kind(self) -> None:
        release = _release([ASR])
        self.assertIs(update_worker.pick_asset(release, "onefile"), ASR)

    def test_no_assets(self) -> None:
        self.assertIsNone(update_worker.pick_asset(_release([]), "folder"))


class InstallKindTests(unittest.TestCase):
    def test_source_mode(self) -> None:
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(update_worker.install_kind(), "source")

    def test_folder_mode_detects_internal(self) -> None:
        fake_exe = Path(self.id().replace(".", "_") + "_app.exe")
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(fake_exe)
        ), mock.patch("pathlib.Path.is_dir", return_value=True):
            self.assertEqual(update_worker.install_kind(), "folder")

    def test_onefile_mode_without_internal(self) -> None:
        fake_exe = Path("onefile_app.exe")
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(fake_exe)
        ), mock.patch("pathlib.Path.is_dir", return_value=False):
            self.assertEqual(update_worker.install_kind(), "onefile")


class WorkerSignalTests(unittest.TestCase):
    def test_download_without_asset_reports_failure(self) -> None:
        worker = update_worker.UpdateDownloadWorker(_release([]))
        seen: list[str] = []
        worker.failed.connect(seen.append)
        worker.run()
        self.assertTrue(seen)
        self.assertIn("安装包", seen[0])


if __name__ == "__main__":
    unittest.main()