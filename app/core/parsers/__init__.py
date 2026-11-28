"""可插拔解析器注册表。

新增格式 = 继承 `BaseDocumentParser` + 在 `PARSERS` 里加一行，批量页与拖拽入口会自动识别。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .base import (
    ALL_STATUSES,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_TEXT,
    BaseDocumentParser,
    ExportMode,
    TranslationUnit,
    parse_failed_units,
)
from .image_batch_parser import ImageBatchParser, natural_key
from .json_i18n_parser import JsonI18nParser
from .markdown_parser import MarkdownParser
from .placeholder_guard import Guard, PlaceholderGuard, protect_batch
from .subtitle_parser import SubtitleParser

PARSERS: tuple[type[BaseDocumentParser], ...] = (
    ImageBatchParser,
    SubtitleParser,
    JsonI18nParser,
    MarkdownParser,
)

_REGISTRY: dict[str, BaseDocumentParser] = {}


def all_parsers() -> list[BaseDocumentParser]:
    """返回解析器实例（按优先级排序，图片优先）。"""
    if not _REGISTRY:
        for cls in PARSERS:
            _REGISTRY.setdefault(cls.parser_id, cls())
    return list(_REGISTRY.values())


def parser_for(path: Path | str) -> BaseDocumentParser | None:
    """按扩展名找解析器；找不到返回 None。"""
    suffix = Path(path).suffix.lower()
    for parser in all_parsers():
        if suffix in parser.supported_exts:
            return parser
    return None


def parser_by_id(parser_id: str) -> BaseDocumentParser | None:
    for parser in all_parsers():
        if parser.parser_id == parser_id:
            return parser
    return None


def supported_extensions() -> tuple[str, ...]:
    exts: list[str] = []
    for parser in all_parsers():
        for ext in parser.supported_exts:
            if ext not in exts:
                exts.append(ext)
    return tuple(exts)


def is_supported(path: Path | str) -> bool:
    p = Path(path)
    return p.is_dir() or parser_for(p) is not None


def group_paths(paths: Iterable[Path | str]) -> list[tuple[BaseDocumentParser, list[Path]]]:
    """把选中的文件按解析器分组：图片合并成一组，其它每种格式按文件各成一组。"""
    images: list[Path] = []
    groups: list[tuple[BaseDocumentParser, list[Path]]] = []
    for raw in paths:
        path = Path(raw)
        parser = parser_for(path)
        if parser is None:
            continue
        if parser.parser_id == ImageBatchParser.parser_id:
            images.append(path)
        else:
            groups.append((parser, [path]))
    if images:
        image_parser = parser_by_id(ImageBatchParser.parser_id) or ImageBatchParser()
        groups.insert(0, (image_parser, sorted(images, key=natural_key)))
    return groups


__all__ = [
    "ALL_STATUSES",
    "BaseDocumentParser",
    "ExportMode",
    "Guard",
    "ImageBatchParser",
    "JsonI18nParser",
    "MarkdownParser",
    "PARSERS",
    "PlaceholderGuard",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_SKIPPED",
    "STATUS_TEXT",
    "SubtitleParser",
    "TranslationUnit",
    "all_parsers",
    "group_paths",
    "is_supported",
    "natural_key",
    "parse_failed_units",
    "parser_by_id",
    "parser_for",
    "protect_batch",
    "supported_extensions",
]
