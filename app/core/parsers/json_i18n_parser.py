"""i18n JSON 解析器：只翻字符串叶子，键结构、顺序与缩进严格保留。

- 导出直接读回原文件再按 meta["path"] 改值，所以嵌套数组 / 深层对象都不会走样；
- 双语模式额外写一份 Markdown 对照表（路径 / 原文 / 译文），JSON 本身仍是合法单语结构。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base import (
    STATUS_DONE,
    BaseDocumentParser,
    ExportMode,
    TranslationUnit,
)
from .placeholder_guard import PlaceholderGuard

_LETTER = re.compile(r"[A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)?%?$")
_SKIP_PREFIX = ("http://", "https://", "mailto:", "tel:", "ftp://", "data:")


class JsonI18nParser(BaseDocumentParser):
    parser_id = "json_i18n"
    display_name = "本地化 JSON"
    supported_exts = (".json",)

    def __init__(self) -> None:
        super().__init__()
        self._guard = PlaceholderGuard()

    # ------------------------------------------------------------------ parse
    def parse(self, file_paths: list[Path]) -> list[TranslationUnit]:
        units: list[TranslationUnit] = []
        index = 0
        for path in file_paths:
            data = json.loads(_read_text(path))
            for pointer, parts, value in _walk(data):
                if not _translatable(value):
                    continue
                protected, mapping = self._guard.protect(value)
                units.append(
                    TranslationUnit(
                        unit_index=index,
                        source_text=value,
                        meta={
                            "file": str(path),
                            "path": parts,
                            "pointer": pointer,
                            "mapping": mapping,
                            "protected": protected,
                            "location": pointer,
                        },
                    )
                )
                index += 1
        if not units:
            raise ValueError("这个 JSON 里没有需要翻译的字符串（可能都是数字、布尔或 URL）。")
        return units

    # ----------------------------------------------------------------- export
    def export(
        self,
        original_paths: list[Path],
        units: list[TranslationUnit],
        output_path: Path,
        mode: ExportMode = "bilingual",
    ) -> Path:
        if not original_paths:
            raise ValueError("没有可导出的源文件。")
        source = original_paths[0]
        raw_text = _read_text(source)
        data = json.loads(raw_text)
        self.last_extras = []

        applied = 0
        rows: list[tuple[str, str, str]] = []
        for unit in sorted(units, key=lambda u: u.unit_index):
            parts = unit.meta.get("path") or []
            if not parts:
                continue
            value = unit.translated_text if unit.status == STATUS_DONE else unit.source_text
            if mode == "bilingual":
                rows.append((str(unit.meta.get("pointer") or "."), unit.source_text, value))
            if unit.status == STATUS_DONE and _set_path(data, list(parts), value):
                applied += 1

        indent = _detect_indent(raw_text)
        output_path = _ensure(output_path, ".json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8"
        )

        if mode == "bilingual" and rows:
            table = output_path.with_name(output_path.stem + ".对照表.md")
            lines = ["# 术语对照（JSON）", "", "| 路径 | 原文 | 译文 |", "| --- | --- | --- |"]
            for pointer, original, translated in rows:
                lines.append(
                    f"| `{pointer}` | {_cell(original)} | {_cell(translated)} |"
                )
            table.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.last_extras = [table]
        return output_path


# --------------------------------------------------------------------- helpers
def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _translatable(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > 4000:
        return False
    if text.lower().startswith(_SKIP_PREFIX):
        return False
    if _NUMBER.match(text):
        return False
    return bool(_LETTER.search(text))


def _walk(node: Any, parts: list[str] | None = None, pointer: str = ""):
    parts = parts or []
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, [*parts, key], f"{pointer}.{key}" if pointer else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, [*parts, str(i)], f"{pointer}[{i}]")
    else:
        yield pointer or ".", parts, node


def _set_path(data: Any, parts: list[str], value: str) -> bool:
    if not parts:
        return False
    node = data
    for key in parts[:-1]:
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return False
        if node is None:
            return False
    last = parts[-1]
    if isinstance(node, dict):
        node[last] = value
        return True
    if isinstance(node, list) and last.isdigit() and int(last) < len(node):
        node[int(last)] = value
        return True
    return False


def _detect_indent(raw_text: str) -> int | None:
    if "\n" not in raw_text:
        return None
    for line in raw_text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if not stripped:
            continue
        spaces = len(line) - len(stripped)
        return spaces or None
    return None


def _ensure(path: Path, suffix: str) -> Path:
    if path.suffix.lower() == suffix:
        return path
    if not path.suffix:
        return path.with_suffix(suffix)
    return path
