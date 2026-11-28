"""生成 updates/latest.json —— GitHub 接口限流时的更新兜底清单。

用法（在仓库根目录）：
    python tools/make_latest_json.py                 # 读当前版本，扫 dist/ 里的安装包
    python tools/make_latest_json.py --notes notes.md  # 附带更新说明（可选）

为什么需要它：api.github.com 对未登录请求按出口 IP 限流（60 次/小时），
国内共享出口十分钟就能用光，用户只会看到「更新检查被限流（HTTP 403）」。
这份静态清单放在仓库里，程序读不到 API 时改读它（jsDelivr → raw），
内容与 Release 保持一致：版本号、包名、体积、SHA-256、下载地址。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import APP_PROJECT, VERSION  # noqa: E402

REPO = "4KAForever11/IndexTranslate-desktop"
DIST = ROOT / "dist"
OUT = ROOT / "updates" / "latest.json"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 updates/latest.json")
    parser.add_argument("--tag", default=f"v{VERSION}", help="发布标签，默认 v{当前版本}")
    parser.add_argument("--notes", default="", help="更新说明文件（markdown）")
    parser.add_argument("--version", default=VERSION, help="版本号，默认取 config.VERSION")
    parser.add_argument("--out", default=str(OUT), help="输出路径")
    args = parser.parse_args()

    tag = args.tag if args.tag.startswith("v") else f"v{args.tag}"
    base = f"https://github.com/{REPO}/releases/download/{tag}"

    names = [
        f"IndexTranslate-desktop-{args.version}-win64.zip",
        f"IndexTranslate-desktop-{args.version}-win64-onefile.exe",
    ]
    assets = []
    missing = []
    for name in names:
        path = DIST / name
        if not path.exists():
            missing.append(name)
            continue
        assets.append(
            {
                "name": name,
                "size": path.stat().st_size,
                "sha256": sha256_of(path),
                "url": f"{base}/{name}",
            }
        )
    if missing:
        print("缺少安装包：", ", ".join(missing), file=sys.stderr)
        print("先跑 build.ps1 -Both", file=sys.stderr)
        return 2

    notes = ""
    if args.notes:
        notes_path = Path(args.notes)
        if not notes_path.is_absolute():
            notes_path = ROOT / notes_path
        notes = notes_path.read_text(encoding="utf-8").strip()

    payload = {
        "app": APP_PROJECT,
        "version": args.version,
        "tag": tag,
        "name": tag,
        "page_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "notes": notes,
        "assets": assets,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"清单: {out}")
    for asset in assets:
        print(f"  - {asset['name']}  {asset['size'] / 1048576:.1f} MB  {asset['sha256'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
