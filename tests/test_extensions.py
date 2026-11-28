"""扩展基础设施测试：清单协议、安装/卸载、Sidecar 通信与生命周期。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from app.core.extensions.manager import ExtensionError, ExtensionManager, sha256_file
from app.core.extensions.manifest import (
    ExtensionManifest,
    ManifestError,
    is_newer,
    parse_version,
    version_tuple,
)
from app.core.extensions.sidecar_client import SidecarClient, SidecarError, SidecarTimeout

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE = FIXTURES / "fake_sidecar.py"


def make_manifest(**overrides) -> dict:
    data = {
        "id": "ext-demo",
        "name": "示例扩展",
        "version": "1.0.0",
        "entry": "main.py",
        "host_min": "1.2.0",
        "methods": ["ping"],
    }
    data.update(overrides)
    return data


def build_zip(path: Path, manifest: dict, *, entry_text: str = "print('hi')") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(str(manifest.get("entry") or "main.py"), entry_text)
    return path


class ManifestTests(unittest.TestCase):
    def test_parse_ok(self) -> None:
        manifest = ExtensionManifest.from_dict(make_manifest())
        self.assertEqual(manifest.id, "ext-demo")
        self.assertEqual(manifest.version, "1.0.0")
        self.assertEqual(manifest.methods, ("ping",))
        self.assertTrue(manifest.requires_host("1.2.0"))
        self.assertFalse(manifest.requires_host("1.1.9"))
        self.assertIn("1.2.0", manifest.host_error("1.1.9"))

    def test_rejects_bad_id(self) -> None:
        for bad in ("e", "ext demo", "../x", "ext_demo", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ManifestError):
                    ExtensionManifest.from_dict(make_manifest(id=bad))

    def test_id_is_normalised_to_lowercase(self) -> None:
        # Windows 目录大小写不敏感，统一成小写可以避免装出两个同 id 的扩展
        manifest = ExtensionManifest.from_dict(make_manifest(id="Ext-Demo"))
        self.assertEqual(manifest.id, "ext-demo")

    def test_rejects_bad_version_and_entry(self) -> None:
        with self.assertRaises(ManifestError):
            ExtensionManifest.from_dict(make_manifest(version="1.0"))
        with self.assertRaises(ManifestError):
            ExtensionManifest.from_dict(make_manifest(entry=""))
        with self.assertRaises(ManifestError):
            ExtensionManifest.from_dict(make_manifest(entry="../evil.exe"))
        with self.assertRaises(ManifestError):
            ExtensionManifest.from_dict(make_manifest(entry="C:/abs.exe"))

    def test_version_helpers(self) -> None:
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(version_tuple("bad"), (0, 0, 0))
        self.assertTrue(is_newer("1.2.0", "1.1.9"))
        self.assertFalse(is_newer("1.2.0", "1.2.0"))

    def test_round_trip_json(self) -> None:
        manifest = ExtensionManifest.from_dict(make_manifest(description="说明"))
        again = ExtensionManifest.from_json(manifest.to_json())
        self.assertEqual(again.id, manifest.id)
        self.assertEqual(again.description, "说明")


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "extensions"
        self.manager = ExtensionManager(self.root)

    def test_install_and_list(self) -> None:
        archive = build_zip(Path(self._tmp.name) / "ext.zip", make_manifest())
        installed = self.manager.install_zip(archive)
        self.assertEqual(installed.manifest.id, "ext-demo")
        self.assertTrue((self.root / "ext-demo" / "main.py").is_file())
        self.assertEqual([item.manifest.id for item in self.manager.installed()], ["ext-demo"])
        self.assertTrue(self.manager.installed()[0].enabled)

    def test_sha256_mismatch_is_rejected(self) -> None:
        archive = build_zip(Path(self._tmp.name) / "ext.zip", make_manifest())
        with self.assertRaises(ExtensionError) as ctx:
            self.manager.install_zip(archive, expected_sha256="0" * 64)
        self.assertIn("SHA-256", str(ctx.exception))
        self.assertFalse((self.root / "ext-demo").exists())

    def test_sha256_match_is_accepted(self) -> None:
        archive = build_zip(Path(self._tmp.name) / "ext.zip", make_manifest())
        digest = sha256_file(archive)
        self.manager.install_zip(archive, expected_sha256=digest)
        self.assertIsNotNone(self.manager.get("ext-demo"))

    def test_host_too_old(self) -> None:
        archive = build_zip(
            Path(self._tmp.name) / "ext.zip", make_manifest(host_min="9.0.0")
        )
        with self.assertRaises(ExtensionError) as ctx:
            self.manager.install_zip(archive, host_version="1.2.0")
        self.assertIn("9.0.0", str(ctx.exception))

    def test_entry_missing(self) -> None:
        manifest = make_manifest(entry="missing.py")
        with zipfile.ZipFile(Path(self._tmp.name) / "bad.zip", "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
        with self.assertRaises(ExtensionError) as ctx:
            self.manager.install_zip(Path(self._tmp.name) / "bad.zip")
        self.assertIn("入口程序", str(ctx.exception))

    def test_zip_slip_rejected(self) -> None:
        path = Path(self._tmp.name) / "evil.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(make_manifest()))
            archive.writestr("main.py", "x")
            archive.writestr("../../evil.txt", "you've been hacked")
        with self.assertRaises(ExtensionError) as ctx:
            self.manager.install_zip(path)
        self.assertIn("非法路径", str(ctx.exception))

    def test_reinstall_replaces_version(self) -> None:
        first = build_zip(Path(self._tmp.name) / "v1.zip", make_manifest(version="1.0.0"))
        self.manager.install_zip(first)
        second = build_zip(Path(self._tmp.name) / "v2.zip", make_manifest(version="1.1.0"))
        self.manager.install_zip(second)
        manifest = self.manager.get("ext-demo").manifest
        self.assertEqual(manifest.version, "1.1.0")
        self.assertEqual(len(list(self.root.glob(".staging-*"))), 0)

    def test_enable_disable_and_uninstall(self) -> None:
        archive = build_zip(Path(self._tmp.name) / "ext.zip", make_manifest())
        self.manager.install_zip(archive)
        self.assertTrue(self.manager.set_enabled("ext-demo", False))
        self.assertEqual(self.manager.enabled(), [])
        self.assertTrue(self.manager.set_enabled("ext-demo", True))
        self.assertEqual(len(self.manager.enabled()), 1)
        self.assertTrue(self.manager.uninstall("ext-demo"))
        self.assertEqual(self.manager.installed(), [])
        self.assertFalse((self.root / "ext-demo").exists())

    def test_index_survives_reload(self) -> None:
        archive = build_zip(Path(self._tmp.name) / "ext.zip", make_manifest())
        self.manager.install_zip(archive)
        self.manager.set_enabled("ext-demo", False)
        reopened = ExtensionManager(self.root)
        self.assertFalse(reopened.get("ext-demo").enabled)

    def test_upgrades_filter(self) -> None:
        archive = build_zip(Path(self._tmp.name) / "ext.zip", make_manifest(version="1.0.0"))
        self.manager.install_zip(archive)
        catalog = [
            {"id": "ext-demo", "version": "1.0.0"},
            {"id": "ext-demo", "version": "1.2.0"},
            {"id": "ext-new", "version": "0.1.0"},
        ]
        upgrades = self.manager.upgrades(catalog)
        versions = sorted(item["version"] for item in upgrades)
        self.assertEqual(versions, ["0.1.0", "1.2.0"])


class SidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SidecarClient(FAKE, name="fake", idle_timeout_sec=0.4)
        self.addCleanup(self.client.stop)

    def test_ping(self) -> None:
        result = self.client.ping()
        self.assertEqual(result["echo"], "pong")
        self.assertTrue(self.client.running)
        self.assertIsNotNone(self.client.pid)

    def test_call_round_trip(self) -> None:
        result = self.client.call("echo", {"text": "你好 sidecar"})
        self.assertEqual(result["text"], "你好 sidecar")

    def test_error_is_forwarded(self) -> None:
        with self.assertRaises(SidecarError) as ctx:
            self.client.call("boom")
        self.assertIn("故意失败", str(ctx.exception))

    def test_unknown_method(self) -> None:
        with self.assertRaises(SidecarError):
            self.client.call("nope")

    def test_stream_events(self) -> None:
        events = list(self.client.stream("stream", {"count": 3}))
        chunks = [item for item in events if item["event"] == "chunk"]
        self.assertEqual(len(chunks), 3)
        self.assertEqual(events[-1]["event"], "result")
        self.assertEqual(events[-1]["data"]["count"], 3)

    def test_subscriber_gets_pushed_events(self) -> None:
        seen: list[dict] = []
        self.client.add_subscriber(seen.append)
        ack = self.client.call("start", {})
        self.assertTrue(ack["started"])
        deadline = time.monotonic() + 3.0
        while len([m for m in seen if m.get("event") == "final"]) < 2:
            if time.monotonic() > deadline:
                break
            time.sleep(0.02)
        finals = [m for m in seen if m.get("event") == "final"]
        self.assertEqual(len(finals), 2)
        self.assertEqual(finals[0]["data"]["text"], "Hello there")
        self.assertEqual(self.client.call("stop")["sentences"], 2)

    def test_timeout(self) -> None:
        with self.assertRaises(SidecarTimeout):
            self.client.call("slow", {"sec": 3}, timeout=0.4)

    def test_idle_reclaim_stops_process(self) -> None:
        self.client.idle_timeout_sec = 0.2
        self.client.ping()
        pid = self.client.pid
        self.assertIsNotNone(pid)
        time.sleep(0.35)
        self.assertTrue(self.client.reclaim_if_idle())
        self.assertIsNone(self.client.pid)
        # 回收后还能再冷启动
        self.assertEqual(self.client.call("echo", {"text": "again"})["text"], "again")

    def test_restart_after_crash(self) -> None:
        self.client.ping()
        first_pid = self.client.pid
        # 模拟扩展崩溃：直接杀掉子进程
        subprocess.run(
            ["taskkill", "/PID", str(first_pid), "/F"],
            capture_output=True,
            check=False,
        )
        time.sleep(0.3)
        self.assertFalse(self.client.running)
        # 下一次调用自动重开
        self.assertEqual(self.client.call("echo", {"text": "back"})["text"], "back")
        self.assertNotEqual(self.client.pid, first_pid)

    def test_missing_entry_raises(self) -> None:
        client = SidecarClient(FIXTURES / "does-not-exist.exe", name="missing")
        self.addCleanup(client.stop)
        with self.assertRaises(SidecarError) as ctx:
            client.ping(timeout=3)
        self.assertIn("不存在", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
