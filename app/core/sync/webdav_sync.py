"""WebDAV 同步：把快照传到坚果云 / Alist / Nextcloud 之类的网盘。

只做四件事：``MKCOL`` 建目录、``PUT`` 上传、``GET`` 下载、``PROPFIND`` 探活。
传输走 ``httpx``（复用主程序的 ``new_client``，因此同样带着 no_proxy 的兜底），
密码用 DPAPI 加密后存在设置里，快照里绝不出现明文密钥。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlparse

import httpx

from ..api_client import new_client
from .snapshot import (
    SyncMetaDB,
    build_snapshot,
    backup_database,
    merge_snapshot,
    parse_snapshot,
    snapshot_json,
)

REMOTE_NAME = "index-translate-sync.json"
PROBE_TIMEOUT = 20.0


class WebDAVError(RuntimeError):
    """同步失败：消息直接面向用户。"""


@dataclass
class SyncResult:
    ok: bool
    message: str
    pushed: int = 0
    pulled: int = 0
    uploaded: bool = False
    downloaded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "pushed": self.pushed,
            "pulled": self.pulled,
            "uploaded": self.uploaded,
            "downloaded": self.downloaded,
        }


def _quote_path(path: str) -> str:
    return "/".join(quote(part) for part in path.strip("/").split("/") if part)


class WebDAVClient:
    """极简 WebDAV 客户端（够用即可，不引入 webdavclient3）。"""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or "").strip()
        self.username = username or ""
        self.password = password or ""
        self.timeout = timeout

    # ------------------------------------------------------------------ 基础
    def _auth(self) -> httpx.BasicAuth | None:
        if self.username or self.password:
            return httpx.BasicAuth(self.username, self.password)
        return None

    def resolve(self, name: str = REMOTE_NAME) -> str:
        base = self.base_url
        if not base:
            raise WebDAVError("还没有填写 WebDAV 地址（设置 → 同步与更新）。")
        if not base.endswith("/"):
            base += "/"
        return urljoin(base, quote(name))

    def _client(self) -> httpx.Client:
        return new_client(self.timeout)

    def probe(self) -> str:
        """探测地址与账号是否可用，返回一句人类可读的结论。"""
        url = self.resolve()
        try:
            with self._client() as client:
                response = client.request(
                    "PROPFIND", url, auth=self._auth(), headers={"Depth": "0"}
                )
        except httpx.HTTPError as exc:
            raise WebDAVError(f"连接失败：{exc}") from exc
        if response.status_code in (200, 207):
            return "可以访问"
        if response.status_code == 404:
            return "目录存在但没有同步文件（首次上传会创建）"
        if response.status_code == 401:
            raise WebDAVError("账号或密码不正确（HTTP 401）。")
        if response.status_code == 403:
            raise WebDAVError("服务器拒绝访问（HTTP 403），请检查目录权限。")
        raise WebDAVError(f"服务器返回 HTTP {response.status_code}。")

    def download(self, name: str = REMOTE_NAME) -> bytes | None:
        url = self.resolve(name)
        try:
            with self._client() as client:
                response = client.get(url, auth=self._auth())
        except httpx.HTTPError as exc:
            raise WebDAVError(f"下载失败：{exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            raise WebDAVError("账号或密码不正确（HTTP 401）。")
        if response.status_code >= 400:
            raise WebDAVError(f"下载失败：HTTP {response.status_code}。")
        return response.content

    def upload(self, data: bytes, name: str = REMOTE_NAME) -> None:
        url = self.resolve(name)
        headers = {"Content-Type": "application/json; charset=utf-8"}
        try:
            with self._client() as client:
                response = client.put(url, content=data, auth=self._auth(), headers=headers)
        except httpx.HTTPError as exc:
            raise WebDAVError(f"上传失败：{exc}") from exc
        if response.status_code >= 400:
            raise WebDAVError(f"上传失败：HTTP {response.status_code}。")

    def ensure_collection(self) -> None:
        """地址末段当目录创建一次；已经存在时服务器通常返回 405，忽略即可。"""
        parsed = urlparse(self.base_url)
        if not parsed.path or parsed.path in ("/", ""):
            return
        url = f"{parsed.scheme}://{parsed.netloc}/{_quote_path(parsed.path)}/"
        try:
            with self._client() as client:
                client.request("MKCOL", url, auth=self._auth())
        except httpx.HTTPError:
            return


class SyncService:
    """把快照、元数据与 WebDAV 串起来的门面。"""

    def __init__(
        self,
        glossary_db,
        *,
        meta_path: Path | str,
        db_path: Path | str,
        settings_store,
        device: str = "",
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._glossary = glossary_db
        self._meta = SyncMetaDB(meta_path)
        self._db_path = Path(db_path)
        self._settings = settings_store
        self._device = device
        self._log = logger or (lambda _msg: None)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 配置
    def _client(self) -> WebDAVClient:
        return WebDAVClient(
            str(self._settings.get("sync_url", "") or ""),
            str(self._settings.get("sync_user", "") or ""),
            self._password(),
        )

    def _password(self) -> str:
        from ..secret_store import SecretError, unprotect

        cipher = str(self._settings.get("sync_password", "") or "")
        if not cipher:
            return ""
        try:
            return unprotect(cipher)
        except SecretError as exc:
            self._log(f"密码解密失败：{exc}")
            return ""

    def set_password(self, plain: str) -> None:
        from ..secret_store import SecretError, protect

        try:
            self._settings.set("sync_password", protect(plain))
        except SecretError as exc:
            raise WebDAVError(f"无法加密密码：{exc}") from exc
        self._settings.save()

    # ------------------------------------------------------------------ 动作
    def _snapshot(self) -> dict[str, Any]:
        values = self._settings.values() if hasattr(self._settings, "values") else {}
        return build_snapshot(
            self._glossary,
            styles=list(self._settings.get("styles", []) or []),
            settings=values,
            meta=self._meta,
            device=self._device,
        )

    def _record_result(self, result: SyncResult) -> SyncResult:
        self._settings.set("sync_last_at", round(time.time(), 3))
        self._settings.set("sync_last_result", result.message)
        self._settings.save()
        return result

    def push(self) -> SyncResult:
        client = self._client()
        snapshot = self._snapshot()
        data = snapshot_json(snapshot)
        try:
            client.ensure_collection()
            client.upload(data)
        except WebDAVError as exc:
            return self._record_result(SyncResult(ok=False, message=str(exc)))
        count = len(snapshot.get("records", []))
        return self._record_result(
            SyncResult(
                ok=True,
                message=f"已上传 {count} 条记录（{len(data) / 1024:.1f} KB）",
                pushed=count,
                uploaded=True,
            )
        )

    def pull(self) -> SyncResult:
        client = self._client()
        try:
            raw = client.download()
        except WebDAVError as exc:
            return self._record_result(SyncResult(ok=False, message=str(exc)))
        if raw is None:
            return self._record_result(
                SyncResult(ok=True, message="云端还没有同步文件，先执行一次上传即可。")
            )
        try:
            snapshot = parse_snapshot(raw)
        except ValueError as exc:
            return self._record_result(SyncResult(ok=False, message=str(exc)))

        with self._lock:
            backup_database(self._db_path)
            stats = merge_snapshot(
                snapshot,
                self._glossary,
                meta=self._meta,
                settings_store=self._settings,
            )
        message = stats.summary()
        if stats.errors:
            self._log(f"同步跳过 {len(stats.errors)} 条：{stats.errors[:3]}")
        return self._record_result(
            SyncResult(
                ok=True,
                message=f"已合并云端快照：{message}",
                pulled=stats.added + stats.updated,
                downloaded=True,
            )
        )

    def sync_now(self) -> SyncResult:
        """先拉后推：先吸收别人的修改，再把自己的写上去。"""
        pulled = self.pull()
        if not pulled.ok:
            return pulled
        pushed = self.push()
        if not pushed.ok:
            return pushed
        return self._record_result(
            SyncResult(
                ok=True,
                message=f"{pulled.message}；{pushed.message}",
                pushed=pushed.pushed,
                pulled=pulled.pulled,
                uploaded=True,
                downloaded=pulled.downloaded,
            )
        )

    def test(self) -> tuple[bool, str]:
        try:
            return True, self._client().probe()
        except WebDAVError as exc:
            return False, str(exc)

    def close(self) -> None:
        self._meta.close()