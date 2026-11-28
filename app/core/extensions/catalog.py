"""扩展在线目录：读取官方仓库里的静态清单，供扩展中心一键安装 / 更新。

清单格式（扩展中心只认这个，别指望向量数据库那种花活）::

    {
      "version": 1,
      "updated": "2026-11-28",
      "extensions": [
        {
          "id": "ext-ocr-overlay",
          "name": "本地 OCR 原位贴片",
          "version": "1.1.0",
          "host_min": "2.0.0",
          "description": "…",
          "url": "https://github.com/.../ext-ocr-overlay-1.1.0.zip",
          "sha256": "…64 位十六进制…",
          "size": 12345678
        }
      ]
    }

设计约束：���单只是「谁、哪个版本、从哪下、哈希是多少」，安装仍然走
``ExtensionManager.install_zip``，所以 SHA-256 校验、zip-slip 防护、宿主版本
闸门一个都不会绕过。目录地址可以改（企业内网 / 镜像），指向哪就装哪，但
哈希对不上会直接拒绝安装。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .manifest import parse_version

CATALOG_VERSION = 1
FETCH_TIMEOUT = 25.0


class CatalogError(RuntimeError):
    """目录拉取或解析失败，message 直接给用户看。"""


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    name: str
    version: str
    url: str
    sha256: str = ""
    host_min: str = "0.0.0"
    description: str = ""
    size: int = 0
    deps: tuple[str, ...] = ()
    notes: str = ""

    @property
    def size_text(self) -> str:
        if self.size <= 0:
            return "未知大小"
        if self.size >= 1024 * 1024:
            return f"{self.size / (1024 * 1024):.1f} MB"
        return f"{max(1, round(self.size / 1024))} KB"

    def host_ok(self, host_version: str) -> bool:
        try:
            return parse_version(host_version) >= parse_version(self.host_min)
        except ValueError:
            return True


def _as_entry(raw: dict[str, Any]) -> CatalogEntry:
    ext_id = str(raw.get("id", "")).strip()
    if not ext_id:
        raise CatalogError("目录里有条目缺少 id 字段。")
    url = str(raw.get("url", "")).strip()
    if not url.lower().startswith(("http://", "https://")):
        raise CatalogError(f"目录条目「{ext_id}」的下载地址不是 http(s)。")
    deps_raw = raw.get("deps") or ()
    if isinstance(deps_raw, str):
        deps = (deps_raw,)
    else:
        deps = tuple(str(item) for item in deps_raw)
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return CatalogEntry(
        id=ext_id,
        name=str(raw.get("name") or ext_id),
        version=str(raw.get("version") or "0.0.0"),
        url=url,
        sha256=str(raw.get("sha256") or "").lower(),
        host_min=str(raw.get("host_min") or "0.0.0"),
        description=str(raw.get("description") or ""),
        size=size,
        deps=deps,
        notes=str(raw.get("notes") or ""),
    )


def parse_catalog(raw: str | bytes | list | dict) -> list[CatalogEntry]:
    """把目录 JSON 解析成条目列表；任何结构问题都抛 CatalogError。"""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CatalogError(f"目录文件不是 UTF-8 编码：{exc}") from exc
    if isinstance(raw, str):
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"目录格式不正确（不是合法 JSON）：{exc}") from exc
    elif isinstance(raw, (list, dict)):
        data = raw
    else:
        raise CatalogError("目录格式不正确（既不是 JSON 文本也不是对象）。")

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        version = data.get("version")
        if version is not None and int(version) > CATALOG_VERSION:
            raise CatalogError(
                f"目录来自更新的版本（v{version}），请先升级本程序。"
            )
        items = data.get("extensions") or data.get("items") or []
    else:
        raise CatalogError("目录格式不正确（顶层既不是对象也不是数组）。")

    if not isinstance(items, list):
        raise CatalogError("目录格式不正确（extensions 不是数组）。")

    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = _as_entry(item)
        if entry.id in seen:
            continue
        seen.add(entry.id)
        entries.append(entry)
    return entries


def catalog_url(default: str = "") -> str:
    from ..config import PROJECT_CATALOG_URL, settings

    value = str(settings.get("extension_catalog", "") or "").strip()
    return value or default or PROJECT_CATALOG_URL


def fetch_catalog(url: str | None = None, *, timeout: float = FETCH_TIMEOUT) -> list[CatalogEntry]:
    """从目录地址拉取清单。失败时抛出带中文说明的 CatalogError。"""
    from ...core.api_client import new_client

    target = (url or catalog_url()).strip()
    if not target:
        raise CatalogError("没有配置扩展目录地址（设置 → 扩展中心 → 目录地址）。")
    if not target.lower().startswith(("http://", "https://")):
        raise CatalogError("目录地址必须以 http:// 或 https:// 开头。")
    try:
        with new_client(timeout) as client:
            response = client.get(target)
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转成用户可读文案
        raise CatalogError(f"无法访问扩展目录：{exc}") from exc

    if response.status_code == 404:
        raise CatalogError("扩展目录不存在（404），可能是仓库还没发布清单。")
    if response.status_code >= 400:
        raise CatalogError(f"扩展目录返回 HTTP {response.status_code}。")
    return parse_catalog(response.content)


def entry_for(entries: Iterable[CatalogEntry], ext_id: str) -> CatalogEntry | None:
    for entry in entries:
        if entry.id == ext_id:
            return entry
    return None
