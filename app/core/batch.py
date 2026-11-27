"""批量翻译：把多张图片 / 多个文档拆成可排队的最小单元。

界面只负责排队与展示，真正切分与合并都在这里，方便单元测试：
* 图片 → 一张一个单元；
* ``.txt`` / ``.md`` → 按空行切段，太短的合并，超长的再切；
* ``.srt`` → 按字幕条切，保留时间轴。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import MAX_INPUT_CHARS

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".srt", ".vtt"}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | TEXT_SUFFIXES

# 单文件最多切这么多段，避免误拖一个巨大日志把队列表塞满
MAX_SEGMENTS_PER_FILE = 400
# 太短的段落合并到一起，减少请求数（也避免模型对半句话乱猜）
MIN_SEGMENT_CHARS = 40

_CUE_TIME = re.compile(r"^\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")
_BLANK = re.compile(r"\n\s*\n")

KIND_IMAGE = "image"
KIND_TEXT = "text"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STATUS_TEXT = {
    STATUS_PENDING: "等待中",
    STATUS_RUNNING: "翻译中",
    STATUS_DONE: "完成",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消",
}


@dataclass
class Segment:
    """One request's worth of content."""

    label: str
    text: str = ""
    image_path: str = ""


@dataclass
class BatchItem:
    path: Path
    kind: str
    status: str = STATUS_PENDING
    segments: list[Segment] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def total(self) -> int:
        return len(self.segments)

    @property
    def finished(self) -> int:
        return sum(1 for result in self.results if result is not None) + sum(
            1 for error in self.errors if error
        )

    @property
    def progress(self) -> float:
        return 1.0 if not self.segments else self.finished / len(self.segments)

    @property
    def summary(self) -> str:
        if self.status == STATUS_FAILED:
            for error in self.errors:
                if error:
                    return error
            return "翻译失败"
        if self.status == STATUS_DONE:
            text = " ".join(result.strip() for result in self.results if result).strip()
            text = " ".join(text.split())
            return text[:80] + ("…" if len(text) > 80 else "")
        if self.status == STATUS_RUNNING:
            return f"{self.finished}/{self.total} 段"
        return ""

    def reset(self) -> None:
        self.status = STATUS_PENDING
        self.results = [None] * len(self.segments)  # type: ignore[list-item]
        self.errors = [""] * len(self.segments)
        self.elapsed_ms = 0

    def render(self, mode: str) -> str:
        """Rendered text for export. ``mode``: bilingual | target | both."""
        if mode == "both":
            sources: list[str] = []
            targets: list[str] = []
            for index, segment in enumerate(self.segments):
                result = (self.results[index] or "").strip()
                if not result:
                    continue
                source = segment.text.strip()
                if not source and segment.image_path:
                    source = f"（图片：{segment.label}）"
                if source:
                    sources.append(source)
                targets.append(result)
            blocks: list[str] = []
            if sources:
                blocks.append("**原文**\n\n" + "\n\n".join(sources))
            if targets:
                blocks.append("**译文**\n\n" + "\n\n".join(targets))
            return "\n\n".join(blocks)

        parts: list[str] = []
        for index, segment in enumerate(self.segments):
            result = (self.results[index] or "").strip()
            if not result:
                continue
            if mode == "target":
                parts.append(result)
                continue
            source = segment.text.strip()
            if segment.image_path:
                source = source or f"（图片：{segment.label}）"
            parts.append(f"{source}\n\n{result}")
        return "\n\n".join(parts)


def item_kind(path: Path | str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return KIND_IMAGE
    return KIND_TEXT


def supported(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _split_long(text: str, limit: int = MAX_INPUT_CHARS) -> list[str]:
    """切成长度受限的块，尽量落在句末标点上。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("。"), window.rfind("！"), window.rfind("？"), window.rfind(". "))
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        chunks.append(remaining.strip())
    return [chunk for chunk in chunks if chunk]


def _paragraphs(text: str) -> list[str]:
    blocks = [block.strip() for block in _BLANK.split(text.replace("\r\n", "\n").replace("\r", "\n"))]
    merged: list[str] = []
    buffer = ""
    for block in blocks:
        if not block:
            continue
        if buffer:
            candidate = f"{buffer}\n\n{block}"
            if len(candidate) <= MAX_INPUT_CHARS and len(buffer) < MIN_SEGMENT_CHARS:
                buffer = candidate
                continue
            merged.append(buffer)
            buffer = block
        else:
            buffer = block
    if buffer:
        merged.append(buffer)
    return merged


def _parse_srt(text: str) -> list[Segment]:
    segments: list[Segment] = []
    current_time = ""
    body: list[str] = []

    def flush() -> None:
        if body:
            segments.append(Segment(label=current_time or f"#{len(segments) + 1}", text="\n".join(body).strip()))

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            flush()
            body = []
            continue
        if _CUE_TIME.match(stripped):
            flush()
            body = []
            current_time = stripped.replace("-->", "→")
            continue
        if stripped.isdigit() and not body:
            continue
        body.append(stripped)
    flush()
    return [segment for segment in segments if segment.text]


def build_item(path: Path | str) -> BatchItem:
    """Turn one file into a queue item (segments, but no translation yet)."""
    target = Path(path)
    kind = item_kind(target)
    if kind == KIND_IMAGE:
        item = BatchItem(path=target, kind=kind)
        item.segments = [Segment(label=target.name, image_path=str(target))]
    else:
        item = BatchItem(path=target, kind=kind)
        text = _read_text(target)
        if target.suffix.lower() in {".srt", ".vtt"}:
            segments = _parse_srt(text)
        else:
            segments = [Segment(label=f"第 {index + 1} 段", text=block) for index, block in enumerate(_paragraphs(text))]
        expanded: list[Segment] = []
        for segment in segments:
            for chunk in _split_long(segment.text):
                expanded.append(Segment(label=segment.label, text=chunk))
            if len(expanded) >= MAX_SEGMENTS_PER_FILE:
                break
        item.segments = expanded[:MAX_SEGMENTS_PER_FILE]
    item.reset()
    return item


def collect_paths(paths: Iterable[Path | str]) -> list[Path]:
    """Expand directories one level deep, keep only supported files, de-dupe."""
    found: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        candidates = [path] if path.is_file() else sorted(p for p in path.glob("*") if p.is_file())
        for candidate in candidates:
            if not supported(candidate):
                continue
            key = str(candidate.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(candidate)
    return found


def export_markdown(items: Iterable[BatchItem], mode: str = "bilingual") -> str:
    """Merge every finished item into one Markdown document."""
    blocks: list[str] = ["# 批量翻译结果", ""]
    for item in items:
        if item.status != STATUS_DONE:
            continue
        blocks.append(f"## {item.name}")
        blocks.append("")
        rendered = item.render(mode)
        blocks.append(rendered or "（无内容）")
        blocks.append("")
    if len(blocks) <= 2:
        return ""
    blocks.append("---")
    blocks.append("")
    blocks.append("*由 IndexTranslate desktop 批量翻译导出*")
    return "\n".join(blocks)
