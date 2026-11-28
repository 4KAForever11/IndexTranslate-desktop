"""生成扩展在线目录 extensions/catalog.json。

扩展包由 build.ps1 -Target extensions 产出到 dist/extensions/，每个 zip 旁边
还有一个 <id>-<version>.json（= manifest + sha256 + host_version）。本脚本把
这些信息汇总成扩展中心读取的静态清单，并把下载地址指向对应的 Release 资源。

用法::

    python tools/make_catalog.py                    # 用 config.VERSION 作标签
    python tools/make_catalog.py --tag v2.1.0       # 显式指定 Release 标签
    python tools/make_catalog.py --base-url https://example.com/ext   # 自建镜像

注意：sha256 必须对应真正上传到 Release 的那份 zip，改了包就要重跑本脚本，
否则扩展中心会按设计拒绝安装（这是防篡改的关键一环）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys_path = ROOT / "app"

REPO = "4KAForever11/IndexTranslate-desktop"
CATALOG_PATH = ROOT / "extensions" / "catalog.json"
DIST = ROOT / "dist" / "extensions"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_current_version() -> str:
    text = (ROOT / "app" / "core" / "config.py").read_text("utf-8")
    for line in text.splitlines():
        if line.startswith("VERSION = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("app/core/config.py 里找不到 VERSION")


def build(base_url: str, tag: str) -> dict:
    if not DIST.is_dir():
        raise SystemExit(f"找不到 {DIST}，先跑 build.ps1 -Target extensions")
    entries = []
    for zip_path in sorted(DIST.glob("*.zip")):
        meta_path = zip_path.with_suffix(".json")
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text("utf-8"))
        digest = meta.get("sha256") or sha256_file(zip_path)
        url = base_url.rstrip("/") + "/" + zip_path.name if base_url else (
            f"https://github.com/{REPO}/releases/download/{tag}/{zip_path.name}"
        )
        entry = {
            "id": meta["id"],
            "name": meta["name"],
            "version": meta["version"],
            "host_min": meta.get("host_version") or meta.get("host_min", "0.0.0"),
            "description": meta.get("description", ""),
            "url": url,
            "sha256": digest,
            "size": zip_path.stat().st_size,
        }
        deps = meta.get("deps") or []
        if deps:
            entry["deps"] = deps
        if meta.get("model_hint"):
            entry["notes"] = meta["model_hint"]
        entries.append(entry)

    if not entries:
        raise SystemExit("dist/extensions 里没有已构建的扩展包")

    return {
        "version": 1,
        "app": "IndexTranslate desktop",
        "tag": tag,
        "extensions": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成扩展在线目录")
    parser.add_argument("--tag", default="")
    parser.add_argument("--base-url", default="", help="自建镜像前缀（留空用 GitHub Release）")
    args = parser.parse_args()

    tag = args.tag or f"v{read_current_version()}"
    catalog = build(args.base_url, tag)
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    total = sum(item["size"] for item in catalog["extensions"]) / 1024 / 1024
    print(f"catalog: {CATALOG_PATH}  ({len(catalog['extensions'])} 个扩展, {total:.1f} MB)")
    for item in catalog["extensions"]:
        print(f"  - {item['id']} {item['version']}  {item['size'] / 1024 / 1024:.1f} MB  {item['sha256'][:12]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
