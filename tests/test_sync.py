"""同步与更新的单元测试（全离线：假 WebDAV 服务器 + 假 GitHub API）。"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.core import updater
from app.core.glossary_db import GlossaryDB
from app.core.sync import (
    SyncMetaDB,
    SyncService,
    WebDAVClient,
    WebDAVError,
    build_snapshot,
    merge_snapshot,
    parse_snapshot,
    snapshot_json,
)
from app.core.updater import Asset, ReleaseInfo, Updater, UpdateError, is_newer, version_key


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: D401 - 静音
        pass


class _FakeSettings:
    """和 config.Settings 同形状的最小替身。"""

    def __init__(self, data: dict | None = None) -> None:
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value) -> None:
        self._data[key] = value

    def save(self) -> None:
        pass

    def values(self) -> dict:
        return dict(self._data)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.a = GlossaryDB(self.tmp / "a.db")
        self.b = GlossaryDB(self.tmp / "b.db")
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.meta_a = SyncMetaDB(self.tmp / "a.meta.db")
        self.meta_b = SyncMetaDB(self.tmp / "b.meta.db")
        self.addCleanup(self.meta_a.close)
        self.addCleanup(self.meta_b.close)
        self.settings_a = _FakeSettings({"styles": [], "target_lang": "zh"})
        self.settings_b = _FakeSettings({"styles": [], "target_lang": "zh"})

    def _snap(self, db, meta, settings, device="dev"):
        return build_snapshot(
            db,
            styles=list(settings.get("styles") or []),
            settings=settings.values(),
            meta=meta,
            device=device,
        )

    def test_snapshot_carries_terms_groups_styles_and_prefs(self) -> None:
        self.a.upsert("光遇", "Sky", "游戏")
        self.settings_a.set("styles", [{"id": "tech", "name": "技术", "text": "保留代码"}])
        snapshot = self._snap(self.a, self.meta_a, self.settings_a)
        kinds = {record["kind"] for record in snapshot["records"]}
        self.assertIn("glossary_term", kinds)
        self.assertIn("glossary_group", kinds)
        self.assertIn("style", kinds)
        self.assertIn("pref", kinds)
        self.assertEqual(snapshot["version"], 1)
        for record in snapshot["records"]:
            self.assertTrue(record["uuid"])
            self.assertGreater(record["updated_at"], 0)

    def test_merge_adds_records_from_the_other_machine(self) -> None:
        self.a.upsert("阶段一", "Season Pass", "游戏")
        snapshot = self._snap(self.a, self.meta_a, self.settings_a)
        stats = merge_snapshot(parse_snapshot(snapshot_json(snapshot)), self.b, meta=self.meta_b)
        self.assertGreaterEqual(stats.added, 1)
        self.assertEqual(self.b.get("阶段一", "游戏"), "Season Pass")

    def test_two_machines_keep_both_sides_records(self) -> None:
        self.a.upsert("光遇", "Sky", "游戏")
        self.b.upsert("限流", "rate limiting", "开发")
        merge_snapshot(
            parse_snapshot(snapshot_json(self._snap(self.a, self.meta_a, self.settings_a))),
            self.b,
            meta=self.meta_b,
        )
        merge_snapshot(
            parse_snapshot(snapshot_json(self._snap(self.b, self.meta_b, self.settings_b))),
            self.a,
            meta=self.meta_a,
        )
        self.assertEqual(self.a.get("限流", "开发"), "rate limiting")
        self.assertEqual(self.b.get("光遇", "游戏"), "Sky")

    def test_newer_local_change_wins(self) -> None:
        self.a.upsert("光遇", "Sky", "游戏")
        merge_snapshot(
            parse_snapshot(snapshot_json(self._snap(self.a, self.meta_a, self.settings_a))),
            self.b,
            meta=self.meta_b,
        )
        time.sleep(0.02)
        self.b.upsert("光遇", "Sky: Children of the Light", "游戏")
        self.meta_b.touch("glossary_term", "游戏|光遇")
        stats = merge_snapshot(
            parse_snapshot(snapshot_json(self._snap(self.a, self.meta_a, self.settings_a))),
            self.b,
            meta=self.meta_b,
        )
        self.assertGreaterEqual(stats.kept, 1)
        self.assertEqual(self.b.get("光遇", "游戏"), "Sky: Children of the Light")

    def test_deletion_propagates_as_tombstone(self) -> None:
        self.a.upsert("季卡", "Season Pass", "游戏")
        merge_snapshot(
            parse_snapshot(snapshot_json(self._snap(self.a, self.meta_a, self.settings_a))),
            self.b,
            meta=self.meta_b,
        )
        self.a.delete("季卡", "游戏")
        snapshot = self._snap(self.a, self.meta_a, self.settings_a)
        tombstones = [record for record in snapshot["records"] if record["deleted"]]
        self.assertTrue(any(record["key"].endswith("|季卡") for record in tombstones))
        stats = merge_snapshot(parse_snapshot(snapshot_json(snapshot)), self.b, meta=self.meta_b)
        self.assertGreaterEqual(stats.deleted, 1)
        self.assertIsNone(self.b.get("季卡", "游戏"))

    def test_styles_and_prefs_merge(self) -> None:
        self.settings_a.set("styles", [{"id": "acg", "name": "口语", "text": "像聊天一样"}])
        self.settings_a.set("target_lang", "en")
        snapshot = self._snap(self.a, self.meta_a, self.settings_a)
        merge_snapshot(
            parse_snapshot(snapshot_json(snapshot)),
            self.b,
            meta=self.meta_b,
            settings_store=self.settings_b,
        )
        self.assertEqual(self.settings_b.get("target_lang"), "en")
        styles = self.settings_b.get("styles") or []
        self.assertEqual(styles[0]["id"], "acg")

    def test_snapshot_rejects_unknown_version(self) -> None:
        raw = json.dumps({"version": 99, "records": []}).encode()
        with self.assertRaises(ValueError):
            parse_snapshot(raw)

    def test_snapshot_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            parse_snapshot(b"not json")
        with self.assertRaises(ValueError):
            parse_snapshot(b'{"version": 1}')


class _FakeWebDAV(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    token = "user:pw"
    store: dict[str, bytes] = {}
    auth_required = True

    def log_message(self, *args):  # noqa: D401 - 静音
        pass

    def _authorized(self) -> bool:
        if not self.auth_required:
            return True
        import base64

        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode()
        except Exception:
            return False
        return raw == self.token

    def _respond(self, status: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_PROPFIND(self) -> None:  # noqa: N802
        if not self._authorized():
            self._respond(401)
            return
        key = self.path.lstrip("/")
        status = 207 if key in self.store else 404
        self._respond(status, b"<d:multistatus/>", "application/xml")

    def do_MKCOL(self) -> None:  # noqa: N802
        self._respond(201 if self._authorized() else 401)

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized():
            self._respond(401)
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.store[self.path.lstrip("/")] = self.rfile.read(length)
        self._respond(201)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._respond(401)
            return
        body = self.store.get(self.path.lstrip("/"))
        if body is None:
            self._respond(404)
            return
        self._respond(200, body, "application/json")


class WebDAVTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeWebDAV.store = {}
        _FakeWebDAV.auth_required = True
        self.server = _QuietServer(("127.0.0.1", 0), _FakeWebDAV)
        self.server.RequestHandlerClass.store = _FakeWebDAV.store
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}/dav"
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_probe_and_upload_download(self) -> None:
        client = WebDAVClient(self.base, "user", "pw")
        self.assertIn("没有同步文件", client.probe())
        client.upload(b'{"hello": 1}')
        self.assertEqual(client.download(), b'{"hello": 1}')

    def test_wrong_password_reports_401(self) -> None:
        client = WebDAVClient(self.base, "user", "bad")
        with self.assertRaises(WebDAVError) as ctx:
            client.probe()
        self.assertIn("401", str(ctx.exception))

    def test_missing_url_is_reported(self) -> None:
        with self.assertRaises(WebDAVError):
            WebDAVClient("", "", "").probe()

    def test_service_roundtrip_between_two_machines(self) -> None:
        glossary_a = GlossaryDB(self.tmp / "a.db")
        glossary_b = GlossaryDB(self.tmp / "b.db")
        self.addCleanup(glossary_a.close)
        self.addCleanup(glossary_b.close)
        settings_a = _FakeSettings({"sync_url": self.base, "styles": []})
        settings_b = _FakeSettings({"sync_url": self.base, "styles": []})
        service_a = SyncService(
            glossary_a,
            meta_path=self.tmp / "a.meta.db",
            db_path=self.tmp / "a.db",
            settings_store=settings_a,
            device="A",
        )
        service_b = SyncService(
            glossary_b,
            meta_path=self.tmp / "b.meta.db",
            db_path=self.tmp / "b.db",
            settings_store=settings_b,
            device="B",
        )
        self.addCleanup(service_a.close)
        self.addCleanup(service_b.close)
        service_a.set_password("pw")
        service_b.set_password("pw")
        settings_a.set("sync_user", "user")
        settings_b.set("sync_user", "user")

        glossary_a.upsert("光遇", "Sky", "游戏")
        pushed = service_a.push()
        self.assertTrue(pushed.ok, pushed.message)
        self.assertTrue(pushed.uploaded)

        pulled = service_b.pull()
        self.assertTrue(pulled.ok, pulled.message)
        self.assertEqual(glossary_b.get("光遇", "游戏"), "Sky")

        glossary_b.upsert("限流", "rate limiting", "开发")
        combined = service_b.sync_now()
        self.assertTrue(combined.ok, combined.message)
        self.assertTrue(service_a.pull().ok)
        self.assertEqual(glossary_a.get("限流", "开发"), "rate limiting")

    def test_password_is_encrypted_in_settings(self) -> None:
        service = SyncService(
            GlossaryDB(self.tmp / "c.db"),
            meta_path=self.tmp / "c.meta.db",
            db_path=self.tmp / "c.db",
            settings_store=_FakeSettings({"sync_url": self.base, "styles": []}),
        )
        self.addCleanup(service.close)
        service.set_password("s3cret")
        stored = service._settings.get("sync_password")
        self.assertTrue(stored)
        self.assertNotIn("s3cret", stored)
        self.assertEqual(service._password(), "s3cret")


class _FakeGitHub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    payload: dict | None = None
    status = 200
    asset_bytes = b"hello-binary"

    def log_message(self, *args):  # noqa: D401 - 静音
        pass

    def _respond(self, status: int, body: bytes, content_type="application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/latest") or "/releases?" in self.path:
            if self.payload is None:
                self._respond(404, b"{}")
                return
            body = json.dumps(self.payload).encode()
            if "/releases?" in self.path:
                body = json.dumps([self.payload]).encode()
            self._respond(200, body)
            return
        if "/download/" in self.path:
            self._respond(200, self.asset_bytes, "application/octet-stream")
            return
        self._respond(404, b"{}")


class UpdaterTests(unittest.TestCase):
    def test_version_helpers(self) -> None:
        self.assertEqual(version_key("v1.2.3"), (1, 2, 3))
        self.assertEqual(version_key("nope"), (0, 0, 0))
        self.assertTrue(is_newer("1.3.0", "1.2.9"))
        self.assertFalse(is_newer("1.2.0", "1.2.0"))
        self.assertFalse(is_newer("1.1.9", "1.2.0"))

    def test_release_asset_selection(self) -> None:
        release = ReleaseInfo(
            tag="v1.3.0",
            version="1.3.0",
            name="1.3.0",
            notes="notes",
            page_url="https://example.com",
            assets=[
                Asset(name="IndexTranslate-desktop-1.3.0-win64.zip", url="u1"),
                Asset(name="IndexTranslate-desktop-1.3.0-win64-onefile.exe", url="u2"),
            ],
        )
        self.assertTrue(release.folder_asset().name.endswith(".zip"))
        self.assertTrue(release.onefile_asset().name.endswith(".exe"))

    def test_check_against_fake_github(self) -> None:
        _FakeGitHub.payload = {
            "tag_name": "v9.9.9",
            "name": "v9.9.9",
            "body": "修了很多东西",
            "html_url": "https://github.com/example/releases/tag/v9.9.9",
            "published_at": "2026-01-01T00:00:00Z",
            "prerelease": False,
            "assets": [
                {
                    "name": "IndexTranslate-desktop-9.9.9-win64.zip",
                    "browser_download_url": "http://127.0.0.1:1/download/x.zip",
                    "size": 3,
                    "digest": "",
                }
            ],
        }
        server = _QuietServer(("127.0.0.1", 0), _FakeGitHub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        client = Updater(api_base=f"http://127.0.0.1:{server.server_port}", current_version="1.2.0")
        status = client.check()
        self.assertTrue(status.ok, status.message)
        self.assertTrue(status.available)
        self.assertEqual(status.release.version, "9.9.9")

    def test_check_when_up_to_date(self) -> None:
        _FakeGitHub.payload = {
            "tag_name": "v1.2.0",
            "name": "1.2.0",
            "body": "",
            "html_url": "https://example.com",
            "assets": [],
        }
        server = _QuietServer(("127.0.0.1", 0), _FakeGitHub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = Updater(api_base=f"http://127.0.0.1:{server.server_port}", current_version="1.2.0")
        status = client.check()
        self.assertTrue(status.ok)
        self.assertFalse(status.available)
        self.assertIn("最新", status.message)

    def test_download_verifies_sha256(self) -> None:
        _FakeGitHub.asset_bytes = b"payload-bytes"
        server = _QuietServer(("127.0.0.1", 0), _FakeGitHub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        import hashlib

        good = hashlib.sha256(b"payload-bytes").hexdigest()
        client = Updater(api_base=base)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = client.download(
                Asset(name="a.zip", url=f"{base}/download/a.zip", size=13, digest=f"sha256:{good}"),
                tmp,
            )
            self.assertEqual(target.read_bytes(), b"payload-bytes")

            with self.assertRaises(UpdateError):
                client.download(
                    Asset(name="b.zip", url=f"{base}/download/b.zip", size=13, digest="sha256:" + "0" * 64),
                    tmp,
                )
            self.assertFalse((Path(tmp) / "b.zip").exists())

    def test_update_script_mentions_wait_and_restart(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            archive = Path(tmp) / "update.zip"
            archive.write_bytes(b"PK\x03\x04")
            client = Updater()
            script = client.write_update_script(
                archive,
                app_dir=Path(tmp) / "app",
                exe_name="IndexTranslate-desktop.exe",
                pid=4321,
            )
            text = script.read_text(encoding="mbcs" if _mbcs_ok() else "utf-8", errors="replace")
            self.assertIn("tasklist", text)
            self.assertIn("4321", text)
            self.assertIn("robocopy", text)
            self.assertIn("IndexTranslate-desktop.exe", text)


def _mbcs_ok() -> bool:
    try:
        "中文".encode("mbcs")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


if __name__ == "__main__":
    unittest.main()