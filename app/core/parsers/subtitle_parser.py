"""字幕解析器（.srt / .vtt）：只用「文本质行」做翻译单元，时间轴逐字节保留。

导出的双语字幕把两种语言塞进同一条字幕的文本区（同一时间轴），播放器表现稳定：
- bilingual（默认）：译文在上、原文在下
- bilingual + 选项 ``original_first``：原文在上、译文在下
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    STATUS_DONE,
    STATUS_SKIPPED,
    BaseDocumentParser,
    ExportMode,
    TranslationUnit,
)
from .placeholder_guard import PlaceholderGuard

_TIMING = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}")
_INDEX_LINE = re.compile(r"^\s*\d+\s*$")
_PURE_PUNCT = re.compile(r"^[\s.\-–—!?,，。！？…·:;'\"()（）\[\]【】]*$")


class SubtitleParser(BaseDocumentParser):
    parser_id = "subtitle"
    display_name = "字幕（SRT / VTT）"
    supported_exts = (".srt", ".vtt")

    def __init__(self) -> None:
        super().__init__()
        self._guard = PlaceholderGuard()

    # ------------------------------------------------------------------ parse
    def parse(self, file_paths: list[Path]) -> list[TranslationUnit]:
        units: list[TranslationUnit] = []
        index = 0
        for path in file_paths:
            text = _read_text(path)
            vtt = path.suffix.lower() == ".vtt"
            blocks = _split_cues(text)
            cue_no = 0
            for raw, kind, timing, index_text in blocks:
                location = f"{path.name} · {timing[:23]}" if timing else f"{path.name} · 头部"
                if kind != "cue":
                    units.append(
                        TranslationUnit(
                            unit_index=index,
                            source_text=raw,
                            status=STATUS_SKIPPED,
                            translated_text=raw,
                            meta={
                                "file": str(path),
                                "kind": kind,
                                "location": location,
                                "vtt": vtt,
                            },
                        )
                    )
                    index += 1
                    continue
                lines = raw.split("\n")
                body_start = [i for i, line in enumerate(lines) if _TIMING.match(line)]
                body = lines[body_start[0] + 1 :] if body_start else []
                source = "\n".join(body)
                protected, mapping = self._guard.protect(source)
                cue_no += 1
                units.append(
                    TranslationUnit(
                        unit_index=index,
                        source_text=source,
                        meta={
                            "file": str(path),
                            "kind": "cue",
                            "cue": cue_no,
                            "timing": timing,
                            "index_text": index_text,
                            "mapping": mapping,
                            "protected": protected,
                            "location": location,
                            "vtt": vtt,
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
        *,
        original_first: bool = False,
    ) -> Path:
        source = original_paths[0] if original_paths else None
        suffix = source.suffix if source else ".srt"
        output_path = _ensure(output_path, suffix)
        ordered = sorted(units, key=lambda u: u.unit_index)
        cues: list[str] = []
        cue_no = 0
        for unit in ordered:
            if unit.meta.get("kind") != "cue":
                cues.append(unit.source_text if unit.status != STATUS_DONE else unit.translated_text)
                continue
            cue_no += 1
            timing = str(unit.meta.get("timing") or "")
            index_text = str(unit.meta.get("index_text") or (cue_no if suffix == ".srt" else ""))
            translated = unit.translated_text if unit.status == STATUS_DONE else unit.source_text
            original = unit.source_text
            if mode == "monolingual" or translated.strip() == original.strip():
                body = translated
            elif original_first:
                body = f"{original}\n{translated}"
            else:
                body = f"{translated}\n{original}"
            head = f"{index_text}\n{timing}" if index_text else timing
            cues.append(f"{head}\n{body}".strip("\n"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n\n".join(cues).strip() + "\n", encoding="utf-8")
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


def _split_cues(text: str) -> list[tuple[str, str, str, str]]:
    """返回 (原始块, 类型, 时间轴行, 序号行)。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", normalized)
    result: list[tuple[str, str, str, str]] = []
    for block in blocks:
        raw = block.strip("\n")
        if not raw.strip():
            continue
        lines = raw.split("\n")
        if not any(_TIMING.match(line) for line in lines):
            first = lines[0].strip().upper()
            kind = "header" if first.startswith("WEBVTT") or first.startswith(("NOTE", "STYLE", "REGION")) else "raw"
            result.append((raw, kind, "", ""))
            continue
        timing_idx = next(i for i, line in enumerate(lines) if _TIMING.match(line))
        timing = lines[timing_idx].strip()
        index_text = lines[timing_idx - 1].strip() if timing_idx > 0 and _INDEX_LINE.match(lines[timing_idx - 1]) else ""
        result.append((raw, "cue", timing, index_text))
    if not any(kind == "cue" for _raw, kind, _t, _i in result):
        raise ValueError("没有识别到任何字幕条目，请确认是标准的 .srt / .vtt 文件。")
    return result


def _ensure(path: Path, suffix: str) -> Path:
    if path.suffix.lower() in SubtitleParser.supported_exts:
        return path
    if not path.suffix:
        return path.with_suffix(suffix)
    return path
