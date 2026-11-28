"""自动更新：查 GitHub Releases、下载校验、生成「退出后替换」的批处理。

为什么不自己覆盖文件：Windows 上正在运行的 exe 是被锁定的，任何进程都无法替换它。
所以这里只**生成一个等待脚本**，等主程序退出后由 shell 完成解压、替换、重启。
脚本内容为纯 ASCII（``chcp 65001`` 之后再回切），避免中文路径在 cmd 里乱码。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .api_client import new_client
from .config import APP_PROJECT, VERSION, data_dir

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "4KAForever11/IndexTranslate-desktop"
USER_AGENT = "IndexTranslate-Updater/1.0"
CHECK_TIMEOUT = 25.0

# GitHub 对未认证请求按出口 IP 限流（每小时 60 次），国内运营商/校园网出口常被上百人共用，
# 403 是常态。所以除了官方 API，还准备了一份由 tools/make_latest_json.py 生成的静态清单，
# 放在仓库里（jsDelivr 有 CDN 缓存、raw 兜底），限流时自动改读它。
MANIFEST_TEMPLATES = (
    "https://cdn.jsdelivr.net/gh/{repo}@main/updates/latest.json",
    "https://raw.githubusercontent.com/{repo}/main/updates/latest.json",
)
CACHE_TTL = 900.0   # 15 分钟内复用上次结果，避免每次启动都去撞限流

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


class UpdateError(RuntimeError):
    """更新失败：消息直接给用户看。"""


def version_key(text: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match((text or "").strip())
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer(candidate: str, current: str = VERSION) -> bool:
    return version_key(candidate) > version_key(current)


@dataclass
class Asset:
    name: str
    url: str
    size: int = 0
    digest: str = ""

    @property
    def is_zip(self) -> bool:
        return self.name.lower().endswith(".zip")

    @property
    def is_exe(self) -> bool:
        return self.name.lower().endswith(".exe")


@dataclass
class ReleaseInfo:
    tag: str
    version: str
    name: str
    notes: str
    page_url: str
    published_at: str = ""
    prerelease: bool = False
    assets: list[Asset] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ReleaseInfo":
        tag = str(data.get("tag_name") or "")
        assets = []
        for item in data.get("assets") or []:
            try:
                assets.append(
                    Asset(
                        name=str(item.get("name") or ""),
                        url=str(item.get("browser_download_url") or ""),
                        size=int(item.get("size") or 0),
                        digest=str(item.get("digest") or ""),
                    )
                )
            except (TypeError, ValueError):
                continue
        return cls(
            tag=tag,
            version=tag.lstrip("v") or "0.0.0",
            name=str(data.get("name") or tag),
            notes=str(data.get("body") or ""),
            page_url=str(data.get("html_url") or ""),
            published_at=str(data.get("published_at") or ""),
            prerelease=bool(data.get("prerelease")),
            assets=assets,
        )

    @classmethod
    def from_manifest(cls, data: dict[str, Any]) -> "ReleaseInfo":
        """解析仓库里的静态清单（updates/latest.json），字段与 ReleaseInfo 对齐。"""
        tag = str(data.get("tag") or data.get("version") or "")
        if tag and not tag.startswith("v"):
            tag = f"v{tag}"
        assets = []
        for item in data.get("assets") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            url = str(item.get("url") or "")
            if not name or not url:
                continue
            assets.append(
                Asset(
                    name=name,
                    url=url,
                    size=int(item.get("size") or 0),
                    digest=str(item.get("sha256") or item.get("digest") or ""),
                )
            )
        return cls(
            tag=tag,
            version=tag.lstrip("v") or "0.0.0",
            name=str(data.get("name") or tag),
            notes=str(data.get("notes") or data.get("body") or ""),
            page_url=str(data.get("page_url") or data.get("html_url") or ""),
            published_at=str(data.get("published_at") or ""),
            prerelease=bool(data.get("prerelease")),
            assets=assets,
        )

    # ------------------------------------------------------------------ 选择
    def folder_asset(self) -> Asset | None:
        for asset in self.assets:
            if asset.is_zip and "win64" in asset.name.lower():
                return asset
        for asset in self.assets:
            if asset.is_zip:
                return asset
        return None

    def onefile_asset(self) -> Asset | None:
        for asset in self.assets:
            lowered = asset.name.lower()
            if asset.is_exe and "onefile" in lowered:
                return asset
        for asset in self.assets:
            if asset.is_exe:
                return asset
        return None


@dataclass
class UpdateStatus:
    ok: bool
    message: str
    release: ReleaseInfo | None = None
    available: bool = False
    checked_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "available": self.available,
            "version": self.release.version if self.release else "",
            "checked_at": self.checked_at,
        }


class Updater:
    """GitHub Releases 检查 + 资产下载 + 替换脚本生成。"""

    def __init__(
        self,
        *,
        repo: str = DEFAULT_REPO,
        current_version: str = VERSION,
        channel: str = "stable",
        timeout: float = CHECK_TIMEOUT,
        api_base: str = GITHUB_API,
        manifest_urls: tuple[str, ...] | None = None,
    ) -> None:
        self.repo = repo
        self.current_version = current_version
        self.channel = channel or "stable"
        self.timeout = timeout
        self.api_base = api_base.rstrip("/")
        self.manifest_urls = (
            manifest_urls
            if manifest_urls is not None
            else tuple(template.format(repo=repo) for template in MANIFEST_TEMPLATES)
        )

    # ------------------------------------------------------------------ 查询
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        }

    @staticmethod
    def _rate_limit_message() -> str:
        return (
            "GitHub 接口限流（HTTP 403）：未登录请求按出口 IP 每小时只给 60 次，"
            "共享网络的出口很容易用光。可以稍后再试，或点「打开发布页」手动下载。"
        )

    def _get(self, path: str) -> Any:
        url = f"{self.api_base}{path}"
        try:
            with new_client(self.timeout) as client:
                response = client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise UpdateError(f"连接更新服务器失败：{exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code in (403, 429):
            raise UpdateError(self._rate_limit_message())
        if response.status_code >= 400:
            raise UpdateError(f"更新检查失败：HTTP {response.status_code}。")
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise UpdateError("更新服务器返回了无法解析的内容。") from exc

    # ------------------------------------------------------------------ 镜像清单
    def _get_manifest(self, url: str) -> dict[str, Any]:
        try:
            with new_client(self.timeout) as client:
                response = client.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            raise UpdateError(f"连接镜像失败：{exc}") from exc
        if response.status_code >= 400:
            raise UpdateError(f"镜像返回 HTTP {response.status_code}。")
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise UpdateError("镜像清单不是合法 JSON。") from exc
        if not isinstance(data, dict) or not (data.get("tag") or data.get("version")):
            raise UpdateError("镜像清单缺少版本号。")
        return data

    def manifest(self) -> ReleaseInfo | None:
        """限流/断网时的兜底：仓库里的静态更新清单。"""
        errors: list[str] = []
        for url in self.manifest_urls:
            try:
                return ReleaseInfo.from_manifest(self._get_manifest(url))
            except UpdateError as exc:
                errors.append(f"{url.split('/')[2]}：{exc}")
        if errors:
            raise UpdateError("；".join(errors))
        return None

    # ------------------------------------------------------------------ 缓存
    def _cache_path(self) -> Path:
        return Path(data_dir()) / "update_cache.json"

    def _read_cache(self, *, max_age: float = CACHE_TTL) -> dict[str, Any] | None:
        try:
            data = json.loads(self._cache_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("current") != self.current_version:
            return None
        try:
            age = time.time() - float(data.get("checked_at") or 0)
        except (TypeError, ValueError):
            return None
        if age < 0 or age > max_age:
            return None
        release = data.get("release")
        return release if isinstance(release, dict) else None

    def _write_cache(self, release: ReleaseInfo) -> None:
        payload = {
            "current": self.current_version,
            "checked_at": round(time.time(), 3),
            "release": {
                "tag": release.tag,
                "version": release.version,
                "name": release.name,
                "notes": release.notes,
                "page_url": release.page_url,
                "published_at": release.published_at,
                "prerelease": release.prerelease,
                "assets": [
                    {"name": asset.name, "url": asset.url, "size": asset.size, "sha256": asset.digest}
                    for asset in release.assets
                ],
            },
        }
        try:
            path = self._cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def latest(self) -> ReleaseInfo | None:
        if self.channel == "beta":
            data = self._get(f"/repos/{self.repo}/releases?per_page=10")
            if not data:
                return None
            for item in data:
                if isinstance(item, dict):
                    release = ReleaseInfo.from_json(item)
                    if is_newer(release.version, self.current_version):
                        return release
            if isinstance(data, list) and data:
                return ReleaseInfo.from_json(data[0])
            return None
        data = self._get(f"/repos/{self.repo}/releases/latest")
        if not isinstance(data, dict):
            return None
        return ReleaseInfo.from_json(data)

    def _status(self, release: ReleaseInfo | None, note: str = "") -> UpdateStatus:
        checked_at = round(time.time(), 3)
        if release is None:
            return UpdateStatus(ok=True, message="还没有发布任何版本。", checked_at=checked_at)
        available = is_newer(release.version, self.current_version)
        if not available:
            return UpdateStatus(
                ok=True,
                message=f"已是最新版本 {self.current_version}。{note}",
                release=release,
                checked_at=checked_at,
            )
        return UpdateStatus(
            ok=True,
            message=f"发现新版本 {release.version}（当前 {self.current_version}）。{note}",
            release=release,
            available=True,
            checked_at=checked_at,
        )

    def check(self, *, force: bool = False, use_cache: bool = True) -> UpdateStatus:
        """查最新版本。

        顺序：本地缓存（15 分钟内）→ GitHub API → 仓库里的静态清单（jsDelivr → raw）。
        ``force=True`` 会跳过缓存和前两步的重试，用于用户手动点「检查更新」。
        """
        if use_cache and not force:
            cached = self._read_cache()
            if cached is not None:
                return self._status(ReleaseInfo.from_manifest(cached), note="（15 分钟内已检查过）")

        api_error: UpdateError | None = None
        release: ReleaseInfo | None = None
        try:
            release = self.latest()
        except UpdateError as exc:
            api_error = exc

        note = ""
        if release is None:
            try:
                release = self.manifest()
                note = "（GitHub 接口不可用，已改用镜像清单）"
                api_error = None
            except UpdateError as exc:
                if api_error is None:
                    api_error = exc
                else:
                    api_error = UpdateError(f"{api_error} 镜像也失败：{exc}")

        if release is None:
            raise api_error or UpdateError("更新检查失败。")
        self._write_cache(release)
        return self._status(release, note)

    # ------------------------------------------------------------------ 下载
    def download(
        self,
        asset: Asset,
        dest_dir: Path | str,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancel=None,
    ) -> Path:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / asset.name
        part = target.with_suffix(target.suffix + ".part")
        digest = hashlib.sha256()
        done = 0
        try:
            with new_client(max(60.0, self.timeout)) as client:
                with client.stream("GET", asset.url, headers=self._headers()) as response:
                    if response.status_code >= 400:
                        raise UpdateError(f"下载失败：HTTP {response.status_code}。")
                    total = int(response.headers.get("content-length") or asset.size or 0)
                    with part.open("wb") as handle:
                        for chunk in response.iter_bytes(65536):
                            if cancel is not None and cancel.is_set():
                                raise UpdateError("下载已取消。")
                            handle.write(chunk)
                            digest.update(chunk)
                            done += len(chunk)
                            if progress is not None:
                                progress(done, total)
        except httpx.HTTPError as exc:
            part.unlink(missing_ok=True)
            raise UpdateError(f"下载失败：{exc}") from exc
        except UpdateError:
            part.unlink(missing_ok=True)
            raise
        os.replace(part, target)
        expected = (asset.digest or "").lower().removeprefix("sha256:")
        if expected and expected != digest.hexdigest():
            target.unlink(missing_ok=True)
            raise UpdateError("下载文件校验失败（SHA-256 不匹配），已删除。")
        return target

    # ------------------------------------------------------------------ 应用
    def write_update_script(
        self,
        downloaded: Path,
        *,
        app_dir: Path | str,
        exe_name: str,
        pid: int | None = None,
        script_dir: Path | str | None = None,
    ) -> Path:
        """生成「等进程退出 → 解压替换 → 重启」的批处理。"""
        downloaded = Path(downloaded)
        app_dir = Path(app_dir)
        pid = int(pid or os.getpid())
        script_dir = Path(script_dir) if script_dir else downloaded.parent
        script_dir.mkdir(parents=True, exist_ok=True)
        script = script_dir / "apply-update.bat"

        if downloaded.suffix.lower() == ".exe":
            body = _bat_replace_exe(downloaded, app_dir / exe_name, app_dir / exe_name, pid)
        else:
            body = _bat_replace_folder(downloaded, app_dir, exe_name, pid)

        script.write_text(body, encoding=_bat_encoding(), errors="replace")
        return script

    @staticmethod
    def launch_script(script: Path) -> None:
        subprocess.Popen(
            ["cmd", "/c", "start", "", str(script)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _bat_encoding() -> str:
    """cmd 按控制台代码页读 .bat；中文系统上 MBSC(=936) 才能让中文路径正常。"""
    try:
        "中文".encode("mbcs")
    except (LookupError, UnicodeEncodeError):  # 非 Windows / 编码不可用
        return "ascii"
    return "mbcs"


def _bat_wait_pid(pid: int) -> str:
    return (
        ":wait\r\n"
        f'tasklist /FI "PID eq {pid}" | find "{pid}" >nul\r\n'
        "if errorlevel 1 goto proceed\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        "goto wait\r\n"
        ":proceed\r\n"
    )


def _bat_replace_folder(archive: Path, app_dir: Path, exe_name: str, pid: int) -> str:
    temp = archive.parent / "update-unpack"
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        + _bat_wait_pid(pid)
        + f'set "ARCHIVE={archive}"\r\n'
        f'set "TARGET={app_dir}"\r\n'
        f'set "UNPACK={temp}"\r\n'
        'if exist "%UNPACK%" rd /s /q "%UNPACK%"\r\n'
        'powershell -NoProfile -ExecutionPolicy Bypass -Command '
        '"Expand-Archive -LiteralPath \'%ARCHIVE%\' -DestinationPath \'%UNPACK%\' -Force"\r\n'
        'for /d %%D in ("%UNPACK%\\*") do set "SRC=%%D"\r\n'
        'if not defined SRC set "SRC=%UNPACK%"\r\n'
        'robocopy "%SRC%" "%TARGET%" /E /NFL /NDL /NJH /NJS /R:3 /W:2 >nul\r\n'
        f'start "" "{app_dir / exe_name}"\r\n'
        'rd /s /q "%UNPACK%" 2>nul\r\n'
        'del "%~f0" >nul 2>&1\r\n'
        "endlocal\r\n"
    )


def _bat_replace_exe(downloaded: Path, target: Path, start: Path, pid: int) -> str:
    temp = downloaded.with_suffix(".new")
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        + _bat_wait_pid(pid)
        + f'copy /y "{downloaded}" "{temp}" >nul\r\n'
        f'move /y "{temp}" "{target}" >nul\r\n'
        f'start "" "{start}"\r\n'
        'del "%~f0" >nul 2>&1\r\n'
        "endlocal\r\n"
    )