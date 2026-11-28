"""扩展安装 / 启停 / 卸载（零重型依赖，只做文件与索引管理）。

扩展都放在数据目录下，天然兼容便携模式::

    <data_dir>/extensions/
    ├─ index.json                  # 已安装记录（版本、启用状态、安装时间）
    ├─ ext-ocr-overlay/
    │  ├─ manifest.json
    │  └─ ext-ocr-overlay.exe
    └─ .staging-xxxx/              # 解压中转目录，成功后原子改名
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .manifest import ExtensionManifest, ManifestError, is_newer
from .sidecar_client import SidecarClient

INDEX_NAME = "index.json"
STAGING_PREFIX = ".staging-"
TRASH_PREFIX = ".trash-"


class ExtensionError(RuntimeError):
    """安装 / 卸载失败（消息可直接展示给用户）。"""


def sha256_file(path: str | Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """挡掉 zip-slip（绝对路径 / ..）与目录穿越。"""
    members: list[zipfile.ZipInfo] = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts or ":" in parts[0]:
            raise ExtensionError(f"扩展包内含非法路径：{info.filename}")
        members.append(info)
    return members


@dataclass
class InstalledExtension:
    manifest: ExtensionManifest
    path: Path
    enabled: bool = True
    installed_at: float = 0.0
    source: str = ""

    @property
    def entry_path(self) -> Path:
        return self.path / self.manifest.entry

    @property
    def size_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        return total

    def client(self, *, idle_timeout_sec: float = 300.0) -> SidecarClient:
        return SidecarClient(
            self.entry_path, name=self.manifest.name, idle_timeout_sec=idle_timeout_sec
        )


class ExtensionManager:
    """管理 ``<data_dir>/extensions`` 下的扩展包。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / INDEX_NAME
        self._index: dict[str, dict[str, Any]] = self._load_index()

    # ---------------------------------------------------------------- 索引
    def _load_index(self) -> dict[str, dict[str, Any]]:
        if not self._index_path.is_file():
            return {}
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        installed = raw.get("installed") if isinstance(raw, dict) else None
        if not isinstance(installed, dict):
            return {}
        return {str(k): v for k, v in installed.items() if isinstance(v, dict)}

    def _save_index(self) -> None:
        payload = {"version": 1, "installed": self._index}
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._index_path)

    # ---------------------------------------------------------------- 查询
    def installed(self) -> list[InstalledExtension]:
        out: list[InstalledExtension] = []
        for ext_id, meta in sorted(self._index.items()):
            folder = self.root / ext_id
            manifest_path = folder / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = ExtensionManifest.load(manifest_path)
            except ManifestError:
                continue
            out.append(
                InstalledExtension(
                    manifest=manifest,
                    path=folder,
                    enabled=bool(meta.get("enabled", True)),
                    installed_at=float(meta.get("installed_at") or 0.0),
                    source=str(meta.get("source") or ""),
                )
            )
        return out

    def get(self, ext_id: str) -> InstalledExtension | None:
        for item in self.installed():
            if item.manifest.id == ext_id:
                return item
        return None

    def enabled(self) -> list[InstalledExtension]:
        return [item for item in self.installed() if item.enabled]

    # ---------------------------------------------------------------- 安装
    def install_zip(
        self,
        zip_path: str | Path,
        *,
        expected_sha256: str | None = None,
        source: str = "",
        host_version: str = "",
    ) -> InstalledExtension:
        """校验 SHA-256 → 解压到中转目录 → 校验清单 → 原子替换。"""
        zip_path = Path(zip_path)
        if not zip_path.is_file():
            raise ExtensionError(f"找不到扩展包：{zip_path.name}")

        if expected_sha256:
            actual = sha256_file(zip_path)
            if actual.lower() != expected_sha256.strip().lower():
                raise ExtensionError(
                    "扩展包校验失败（SHA-256 不匹配），可能下载不完整或被篡改。"
                )

        staging = self.root / f"{STAGING_PREFIX}{uuid.uuid4().hex[:8]}"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    members = _safe_members(archive)
                    if not members:
                        raise ExtensionError("扩展包是空的。")
                    archive.extractall(staging, members=members)
            except zipfile.BadZipFile as exc:
                raise ExtensionError(f"扩展包不是合法的 zip：{exc}") from exc

            manifest_path = staging / "manifest.json"
            if not manifest_path.is_file():
                # 允许包里多套一层目录
                candidates = list(staging.glob("*/manifest.json"))
                if len(candidates) == 1:
                    inner = candidates[0].parent
                    for child in inner.iterdir():
                        shutil.move(str(child), str(staging / child.name))
                    shutil.rmtree(inner, ignore_errors=True)
                    manifest_path = staging / "manifest.json"
            if not manifest_path.is_file():
                raise ExtensionError("扩展包缺少 manifest.json。")

            manifest = ExtensionManifest.load(manifest_path)
            if host_version and not manifest.requires_host(host_version):
                raise ExtensionError(manifest.host_error(host_version))
            if not (staging / manifest.entry).is_file():
                raise ExtensionError(
                    f"扩展包里找不到入口程序 {manifest.entry}，请确认清单与文件一致。"
                )

            target = self.root / manifest.id
            trash = None
            if target.exists():
                trash = self.root / f"{TRASH_PREFIX}{uuid.uuid4().hex[:8]}"
                os.replace(target, trash)
            os.replace(staging, target)

            self._index[manifest.id] = {
                "version": manifest.version,
                "enabled": self._index.get(manifest.id, {}).get("enabled", True),
                "installed_at": time.time(),
                "source": source or zip_path.name,
            }
            self._save_index()
            if trash is not None:
                shutil.rmtree(trash, ignore_errors=True)
            return InstalledExtension(
                manifest=manifest,
                path=target,
                enabled=bool(self._index[manifest.id]["enabled"]),
                installed_at=float(self._index[manifest.id]["installed_at"]),
                source=str(self._index[manifest.id]["source"]),
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    # ---------------------------------------------------------------- 状态
    def set_enabled(self, ext_id: str, enabled: bool) -> bool:
        if ext_id not in self._index:
            return False
        self._index[ext_id]["enabled"] = bool(enabled)
        self._save_index()
        return True

    def uninstall(self, ext_id: str) -> bool:
        folder = self.root / ext_id
        removed = False
        if folder.exists():
            trash = self.root / f"{TRASH_PREFIX}{uuid.uuid4().hex[:8]}"
            try:
                os.replace(folder, trash)
                shutil.rmtree(trash, ignore_errors=True)
                removed = True
            except OSError as exc:
                raise ExtensionError(f"删除扩展目录失败：{exc}") from exc
        if ext_id in self._index:
            self._index.pop(ext_id, None)
            self._save_index()
            removed = True
        return removed

    def cleanup_leftovers(self) -> int:
        """清掉上次崩溃留下的中转/垃圾桶目录。"""
        count = 0
        for child in self.root.iterdir():
            if child.is_dir() and child.name.startswith((STAGING_PREFIX, TRASH_PREFIX)):
                shutil.rmtree(child, ignore_errors=True)
                count += 1
        return count

    # ---------------------------------------------------------------- 目录
    def upgrades(self, catalog: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """挑出目录里比已装版本新的条目。"""
        current = {item.manifest.id: item.manifest.version for item in self.installed()}
        out: list[dict[str, Any]] = []
        for entry in catalog:
            if not isinstance(entry, dict):
                continue
            ext_id = str(entry.get("id") or "").strip().lower()
            version = str(entry.get("version") or "")
            if not ext_id or not version:
                continue
            if ext_id not in current or is_newer(version, current[ext_id]):
                out.append(entry)
        return out


def download_extension(
    url: str,
    dest: str | Path,
    *,
    timeout: float = 120.0,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """下载扩展包（复用宿主统一的 httpx 客户端，自带 trust_env 兜底）。"""
    from ..api_client import new_client  # 延迟导入，避免核心模块互相牵连

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with new_client(timeout) as client:
        with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            done = 0
            with open(tmp, "wb") as handle:
                for chunk in response.iter_bytes(1 << 16):
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
                    if cancel and cancel():
                        raise ExtensionError("已取消下载。")
    os.replace(tmp, dest)
    return dest
