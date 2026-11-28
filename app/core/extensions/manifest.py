"""扩展清单协议。

一个扩展包就是一个 zip，根目录下有 ``manifest.json`` 和入口程序：:

    ext-ocr-overlay.zip
    ├─ manifest.json
    └─ ext-ocr-overlay.exe        # 或 main.py（开发模式）

清单字段（全部必填，除标注外）：

``id``           扩展唯一标识，建议 ``ext-xxx``，只用小写字母/数字/连字符
``name``         展示名
``version``      语义化版本，如 ``1.0.0``
``entry``        入口程序文件名（相对 zip 根）
``host_min``     需要的宿主最低版本，如 ``1.3.0``
``description``  一句话说明（可选）
``methods``      支持的 JSONL 方法名列表（可选）
``sha256``       扩展包 zip 的 SHA-256（可选；由扩展目录提供时用于校验）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class ManifestError(ValueError):
    """清单不合法（消息直接展示给用户）。"""


def parse_version(text: str) -> tuple[int, int, int]:
    """"1.2.3" -> (1, 2, 3)；非法版本抛 ManifestError。"""
    match = VERSION_PATTERN.match(str(text or "").strip())
    if not match:
        raise ManifestError(f"版本号「{text}」不是 主.次.修订 的形式。")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def version_tuple(text: str) -> tuple[int, int, int]:
    """宽松版本解析：解析失败按 0.0.0 处理（用于比较已装扩展与目录版本）。"""
    try:
        return parse_version(text)
    except ManifestError:
        return 0, 0, 0


def is_newer(candidate: str, current: str) -> bool:
    return version_tuple(candidate) > version_tuple(current)


@dataclass(frozen=True)
class ExtensionManifest:
    id: str
    name: str
    version: str
    entry: str
    host_min: str
    description: str = ""
    methods: tuple[str, ...] = ()
    sha256: str = ""
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    # ---------------------------------------------------------------- 解析
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExtensionManifest":
        if not isinstance(raw, dict):
            raise ManifestError("manifest.json 必须是一个 JSON 对象。")

        ext_id = str(raw.get("id") or "").strip().lower()
        if not ID_PATTERN.match(ext_id):
            raise ManifestError(
                "扩展 id 只能包含小写字母、数字与连字符（例如 ext-ocr-overlay）。"
            )

        name = str(raw.get("name") or "").strip() or ext_id
        version = str(raw.get("version") or "").strip()
        parse_version(version)  # 校验

        entry = str(raw.get("entry") or "").strip()
        if not entry:
            raise ManifestError("清单缺少 entry（入口程序文件名）。")
        entry_path = Path(entry)
        if entry_path.is_absolute() or ".." in entry_path.parts:
            raise ManifestError("entry 必须是扩展包内的相对路径。")

        host_min = str(raw.get("host_min") or "0.0.0").strip()
        parse_version(host_min)

        methods_raw = raw.get("methods") or []
        if isinstance(methods_raw, str):
            methods_raw = [methods_raw]
        methods = tuple(str(item).strip() for item in methods_raw if str(item).strip())

        description = str(raw.get("description") or "").strip()
        sha256 = str(raw.get("sha256") or "").strip().lower()

        known = {"id", "name", "version", "entry", "host_min", "description", "methods", "sha256"}
        extra = {k: v for k, v in raw.items() if k not in known}
        return cls(
            id=ext_id,
            name=name,
            version=version,
            entry=entry,
            host_min=host_min,
            description=description,
            methods=methods,
            sha256=sha256,
            extra=extra,
        )

    @classmethod
    def from_json(cls, text: str) -> "ExtensionManifest":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest.json 不是合法 JSON：{exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def load(cls, path: str | Path) -> "ExtensionManifest":
        path = Path(path)
        if not path.is_file():
            raise ManifestError(f"找不到清单文件：{path.name}")
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ManifestError(f"读取清单失败：{exc}") from exc
        return cls.from_json(text)

    # ---------------------------------------------------------------- 输出
    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "entry": self.entry,
            "host_min": self.host_min,
        }
        if self.description:
            data["description"] = self.description
        if self.methods:
            data["methods"] = list(self.methods)
        if self.sha256:
            data["sha256"] = self.sha256
        data.update(self.extra)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    # ---------------------------------------------------------------- 兼容
    def requires_host(self, host_version: str) -> bool:
        """宿主版本是否满足 host_min。"""
        return version_tuple(host_version) >= version_tuple(self.host_min)

    def host_error(self, host_version: str) -> str:
        return (
            f"扩展「{self.name}」需要宿主 {self.host_min} 及以上，"
            f"当前为 {host_version}，请先升级 IndexTranslate desktop。"
        )

    def supports(self, method: str) -> bool:
        return not self.methods or method in self.methods


def load_manifests(paths: Iterable[str | Path]) -> list[ExtensionManifest]:
    """批量读取，跳过错文件但记录原因。"""
    out: list[ExtensionManifest] = []
    for path in paths:
        try:
            out.append(ExtensionManifest.load(path))
        except ManifestError:
            continue
    return out
