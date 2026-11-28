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
from .config import VERSION, APP_PROJECT

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "4KAForever11/IndexTranslate-desktop"
USER_AGENT = "IndexTranslate-Updater/1.0"
CHECK_TIMEOUT = 25.0

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
    ) -> None:
        self.repo = repo
        self.current_version = current_version
        self.channel = channel or "stable"
        self.timeout = timeout
        self.api_base = api_base.rstrip("/")

    # ------------------------------------------------------------------ 查询
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        }

    def _get(self, path: str) -> Any:
        url = f"{self.api_base}{path}"
        try:
            with new_client(self.timeout) as client:
                response = client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise UpdateError(f"连接更新服务器失败：{exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code == 403:
            raise UpdateError("更新检查被限流（HTTP 403），请稍后再试。")
        if response.status_code >= 400:
            raise UpdateError(f"更新检查失败：HTTP {response.status_code}。")
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise UpdateError("更新服务器返回了无法解析的内容。") from exc

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

    def check(self) -> UpdateStatus:
        checked_at = round(time.time(), 3)
        release = self.latest()
        if release is None:
            return UpdateStatus(
                ok=True,
                message="还没有发布任何版本。",
                checked_at=checked_at,
            )
        available = is_newer(release.version, self.current_version)
        if not available:
            return UpdateStatus(
                ok=True,
                message=f"已是最新版本 {self.current_version}。",
                release=release,
                checked_at=checked_at,
            )
        return UpdateStatus(
            ok=True,
            message=f"发现新版本 {release.version}（当前 {self.current_version}）。",
            release=release,
            available=True,
            checked_at=checked_at,
        )

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