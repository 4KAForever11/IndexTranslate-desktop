"""Markdown / 纯文本解析器：按「块」翻译，代码块与 Frontmatter 原样透传。

块的划分规则与导出时的重组规则一一对应（都按 unit_index 顺序），因此导出不需要
重新解析源文件，也不会把代码块搬错位置。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import MAX_INPUT_CHARS
from .base import (
    STATUS_DONE,
    STATUS_SKIPPED,
    BaseDocumentParser,
    ExportMode,
    TranslationUnit,
)
from .placeholder_guard import PlaceholderGuard

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_LETTER = re.compile(r"[A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_SENTENCE_END = re.compile(r"(?<=[。！？；.!?;:：])\s*")


class MarkdownParser(BaseDocumentParser):
    parser_id = "markdown"
    display_name = "Markdown / 文本"
    supported_exts = (".md", ".markdown", ".txt", ".text")

    def __init__(self) -> None:
        super().__init__()
        self._guard = PlaceholderGuard()

    # ------------------------------------------------------------------ parse
    def parse(self, file_paths: list[Path]) -> list[TranslationUnit]:
        units: list[TranslationUnit] = []
        index = 0
        for path in file_paths:
            text = _read_text(path)
            for block_no, (raw, translatable) in enumerate(_split_blocks(text)):
                location = f"{path.name} · 块 {block_no + 1}"
                if not translatable:
                    units.append(
                        TranslationUnit(
                            unit_index=index,
                            source_text=raw,
                            status=STATUS_SKIPPED,
                            translated_text=raw,
                            meta={"file": str(path), "block": block_no, "location": location},
                        )
                    )
                    index += 1
                    continue
                parts = _split_text(raw, MAX_INPUT_CHARS)
                for part_no, part in enumerate(parts):
                    protected, mapping = self._guard.protect(part)
                    units.append(
                        TranslationUnit(
                            unit_index=index,
                            source_text=raw if len(parts) == 1 else part,
                            meta={
                                "file": str(path),
                                "block": block_no,
                                "part": part_no,
                                "parts": len(parts),
                                "mapping": mapping,
                                "protected": protected,
                                "location": location,
                            },
                        )
                    )
                    index += 1
        return units

    # ----------------------------------------------------------------- export
    def export(
        self,
        original_paths: list[Path],
        units: list[TranslationUnit],
        output_path: Path,
        mode: ExportMode = "bilingual",
    ) -> Path:
        if not units:
            raise ValueError("没有可导出的翻译结果。")
        # 纯文本/Markdown 的成稿完全由单元重建，源文件只用来取后缀与文件名，
        # 所以源文件已被移走也能重新导出。
        if original_paths:
            path = original_paths[0]
        else:
            path = Path(str(units[0].meta.get("file", "export.md")))
        blocks = self._collect_blocks(units, str(path))
        lines = _render_blocks(blocks, mode)
        output_path = _ensure_suffix(output_path, path.suffix or ".md")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output_path

    # ------------------------------------------------------------------ utils
    def _collect_blocks(
        self, units: list[TranslationUnit], file_key: str
    ) -> list[tuple[int, list[TranslationUnit]]]:
        grouped: dict[int, list[TranslationUnit]] = {}
        for unit in units:
            if str(unit.meta.get("file", "")) not in (file_key, ""):
                continue
            grouped.setdefault(int(unit.meta.get("block", unit.unit_index)), []).append(unit)
        return [
            (block_no, sorted(items, key=lambda u: int(u.meta.get("part", 0))))
            for block_no, items in sorted(grouped.items())
        ]


# --------------------------------------------------------------------- helpers
def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _is_translatable(block: str) -> bool:
    if _FENCE.match(block):
        return False
    if not _LETTER.search(block):
        return False
    if _TABLE_SEP.match(block) and len(block.strip()) < 40:
        return False
    return True


def _split_blocks(text: str) -> list[tuple[str, bool]]:
    """按空行切块；围栏代码块整段算一个块，Frontmatter 单独算一个块。"""
    lines = text.splitlines()
    blocks: list[tuple[str, bool]] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            raw = "\n".join(buffer)
            if raw.strip():
                blocks.append((raw, _is_translatable(raw)))
            buffer.clear()

    idx = 0
    if lines and lines[0].strip() == "---":
        # YAML Frontmatter：找到结束的 --- 之前整体跳过
        for end in range(1, len(lines)):
            if lines[end].strip() in ("---", "..."):
                blocks.append(("\n".join(lines[: end + 1]), False))
                idx = end + 1
                break
        else:
            idx = 0

    in_fence = False
    fence_marker = ""
    while idx < len(lines):
        line = lines[idx]
        match = _FENCE.match(line)
        if not in_fence and match:
            flush()
            in_fence = True
            fence_marker = match.group(1)
            buffer.append(line)
            idx += 1
            continue
        if in_fence:
            buffer.append(line)
            if line.strip().startswith(fence_marker):
                in_fence = False
                flush()
            idx += 1
            continue
        if not line.strip():
            flush()
            idx += 1
            continue
        buffer.append(line)
        idx += 1
    flush()
    return blocks


def _split_text(text: str, limit: int) -> list[str]:
    """把超长块切成若干段，保证 "".join(parts) == text，导出时直接拼接还原。"""
    if len(text) <= limit:
        return [text]
    sentences = [part for part in _SENTENCE_END.split(text) if part]
    if not sentences:
        sentences = [text[i : i + limit] for i in range(0, len(text), limit)]
    parts: list[str] = []
    buffer = ""
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) > limit:
            parts.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        parts.append(buffer)
    return parts


def _unit_output(unit: TranslationUnit, mode: ExportMode) -> str:
    original = unit.source_text
    if unit.status != STATUS_DONE or not unit.translated_text.strip():
        return original
    if mode == "monolingual":
        return unit.translated_text
    return f"{original}\n\n{unit.translated_text}"


def _render_blocks(
    blocks: list[tuple[int, list[TranslationUnit]]], mode: ExportMode
) -> list[str]:
    rendered: list[str] = []
    for _block_no, units in blocks:
        if not units:
            continue
        if units[0].status == STATUS_SKIPPED:
            rendered.append(units[0].source_text)
            continue
        if mode == "monolingual":
            rendered.append("".join(_unit_output(unit, mode) for unit in units))
        else:
            rendered.append("\n\n".join(_unit_output(unit, mode) for unit in units))
    return rendered


def _ensure_suffix(path: Path, suffix: str) -> Path:
    if path.suffix.lower() in MarkdownParser.supported_exts:
        return path
    if not path.suffix:
        return path.with_suffix(suffix)
    return path
