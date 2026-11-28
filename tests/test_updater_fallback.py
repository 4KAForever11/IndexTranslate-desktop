"""更新检查的限流回退 / 本地缓存（不联网）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from app.core.updater import Asset, ReleaseInfo, UpdateError, Updater

MANIFEST = {
    "app": "IndexTranslate desktop",
    "version": "9.9.9",
    "tag": "v9.9.9",
    "name": "v9.9.9",
    "page_url": "https://example.invalid/releases/tag/v9.9.9",
    "notes": "修好了限流",
    "assets": [
        {
            "name": "IndexTranslate-desktop-9.9.9-win64.zip",
            "size": 123,
            "sha256": "a" * 64,
            "url": "https://example.invalid/a.zip",
        }
    ],
}

FOLDER = Asset(name="IndexTranslate-desktop-9.9.9-win64.zip", url="u1", size=10)


def _api_release() -> ReleaseInfo:
    return ReleaseInfo(
        tag="v9.9.9",
        version="9.9.9",
        name="9.9.9",
        notes="",
        page_url="https://example.invalid",
        assets=[FOLDER],
    )


class ManifestTests(unittest.TestCase):
    def test_from_manifest_maps_fields(self) -> None:
        release = ReleaseInfo.from_manifest(dict(MANIFEST))
        self.assertEqual(release.tag, "v9.9.9")
        self.assertEqual(release.version, "9.9.9")
        self.assertEqual(release.notes, "修好了限流")
        self.assertEqual(len(release.assets), 1)
        self.assertEqual(release.assets[0].size, 123)
        self.assertEqual(release.assets[0].digest, "a" * 64)

    def test_from_manifest_adds_v_prefix(self) -> None:
        release = ReleaseInfo.from_manifest({"version": "3.1.0", "assets": []})
        self.assertEqual(release.tag, "v3.1.0")

    def test_rate_limit_message_is_actionable(self) -> None:
        message = Updater._rate_limit_message()
        self.assertIn("限流", message)
        self.assertIn("打开发布页", message)


class FallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("test_updater_fallback_tmp")
        self.tmp.mkdir(exist_ok=True)
        patcher = mock.patch("app.core.updater.data_dir", return_value=str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        for item in sorted(self.tmp.glob("*")):
            item.unlink(missing_ok=True)
        self.tmp.rmdir()

    def test_rate_limited_api_falls_back_to_manifest(self) -> None:
        updater = Updater(current_version="2.0.0")
        with mock.patch.object(
            updater, "_get", side_effect=UpdateError(updater._rate_limit_message())
        ), mock.patch.object(updater, "_get_manifest", return_value=dict(MANIFEST)):
            status = updater.check(force=True)
        self.assertTrue(status.ok)
        self.assertTrue(status.available)
        self.assertEqual(status.release.version, "9.9.9")
        self.assertIn("镜像清单", status.message)

    def test_manifest_only_used_when_api_fails(self) -> None:
        updater = Updater(current_version="2.0.0")
        with mock.patch.object(updater, "latest", return_value=_api_release()), mock.patch.object(
            updater, "_get_manifest"
        ) as manifest:
            status = updater.check(force=True)
        manifest.assert_not_called()
        self.assertNotIn("镜像清单", status.message)

    def test_both_sources_failing_reports_both(self) -> None:
        updater = Updater(current_version="2.0.0")
        with mock.patch.object(updater, "_get", side_effect=UpdateError("限流了")), mock.patch.object(
            updater, "_get_manifest", side_effect=UpdateError("镜像挂了")
        ):
            with self.assertRaises(UpdateError) as ctx:
                updater.check(force=True)
        self.assertIn("限流了", str(ctx.exception))
        self.assertIn("镜像也失败", str(ctx.exception))


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path("test_updater_cache_tmp")
        self.tmp.mkdir(exist_ok=True)
        patcher = mock.patch("app.core.updater.data_dir", return_value=str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        for item in sorted(self.tmp.glob("*")):
            item.unlink(missing_ok=True)
        self.tmp.rmdir()

    def _prime(self, version: str = "2.0.0") -> None:
        updater = Updater(current_version=version)
        with mock.patch.object(updater, "_get", side_effect=UpdateError("限流了")), mock.patch.object(
            updater, "_get_manifest", return_value=dict(MANIFEST)
        ):
            updater.check(force=True)

    def test_second_check_within_ttl_uses_cache(self) -> None:
        self._prime()
        fresh = Updater(current_version="2.0.0")
        with mock.patch.object(fresh, "_get") as api, mock.patch.object(
            fresh, "_get_manifest"
        ) as manifest:
            status = fresh.check()
        api.assert_not_called()
        manifest.assert_not_called()
        self.assertTrue(status.available)
        self.assertIn("已检查过", status.message)

    def test_force_ignores_cache(self) -> None:
        self._prime()
        fresh = Updater(current_version="2.0.0")
        with mock.patch.object(fresh, "_get", side_effect=UpdateError("限流了")), mock.patch.object(
            fresh, "_get_manifest", return_value=dict(MANIFEST)
        ):
            status = fresh.check(force=True)
        self.assertTrue(status.available)

    def test_cache_ignored_when_current_version_changed(self) -> None:
        self._prime("2.0.0")
        fresh = Updater(current_version="2.0.1")
        with mock.patch.object(fresh, "_get") as api, mock.patch.object(
            fresh, "_get_manifest", return_value=dict(MANIFEST)
        ):
            fresh.check()
        api.assert_called()

    def test_stale_cache_is_ignored(self) -> None:
        self._prime()
        fresh = Updater(current_version="2.0.0")
        with mock.patch.object(fresh, "_read_cache", return_value=None) as reader, mock.patch.object(
            fresh, "_get", side_effect=UpdateError("限流了")
        ), mock.patch.object(fresh, "_get_manifest", return_value=dict(MANIFEST)):
            fresh.check()
        reader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
