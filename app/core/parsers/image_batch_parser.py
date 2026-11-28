"""多图批量解析器：一批图片 = 一个任务，导出为 Markdown / 纯文本汇总。

解析阶段只记录路径，绝不预读图片——真正的压缩与 Base64 由调度器消费到该单元时才做，
所以一次拖入 50 张大图也不会把内存顶起来。
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    STATUS_DONE,
    BaseDocumentParser,
    ExportMode,
    TranslationUnit,
)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
_NUM = re.compile(r"(\d+)")


def natural_key(path: Path | str) -> tuple:
    """按文件名里的数字自然排序：page2 < page10。"""
    name = Path(path).name.lower()
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in _NUM.split(name)
    )


class ImageBatchParser(BaseDocumentParser):
    parser_id = "image_batch"
    display_name = "多图批量"
    supported_exts = IMAGE_EXTS

    def parse(self, file_paths: list[Path]) -> list[TranslationUnit]:
        ordered = sorted((Path(p) for p in file_paths), key=natural_key)
        units: list[TranslationUnit] = []
        for index, path in enumerate(ordered):
            units.append(
                TranslationUnit(
                    unit_index=index,
                    source_text="",
                    image_path=str(path),
                    meta={
                        "file": str(path),
                        "location": path.name,
                        "natural_index": index,
                    },
                )
            )
        return units

    def export(
        self,
        original_paths: list[Path],
        units: list[TranslationUnit],
        output_path: Path,
        mode: ExportMode = "bilingual",
    ) -> Path:
        ordered = sorted(units, key=lambda u: u.unit_index)
        as_text = output_path.suffix.lower() == ".txt"
        if not output_path.suffix:
            output_path = output_path.with_suffix(".md")
            as_text = False
        lines: list[str] = []
        if not as_text:
            lines += ["# 图片翻译汇总", ""]
            lines.append(f"共 {len(ordered)} 张，导出模式：{mode}")
            lines.append("")

        for unit in ordered:
            image_path = Path(str(unit.image_path)) if unit.image_path else None
            name = image_path.name if image_path else f"图片 {unit.unit_index + 1}"
            if as_text:
                lines.append(f"== {name} ==")
                text = unit.translated_text if unit.status == STATUS_DONE else unit.error_msg or "（未翻译）"
                lines.append(text)
                lines.append("")
                continue

            lines.append(f"## {name}")
            if image_path is not None:
                if mode != "monolingual":
                    rel = _relative(image_path, output_path)
                    lines.append(f"![{name}]({rel})")
                else:
                    lines.append(f"> 源文件：`{name}`")
            if unit.status == STATUS_DONE and unit.translated_text.strip():
                lines.append("")
                lines.append(unit.translated_text)
            elif unit.error_msg:
                lines.append("")
                lines.append(f"> ⚠️ 失败：{unit.error_msg}")
            else:
                lines.append("")
                lines.append("> （未翻译）")
            lines.append("")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return output_path


def _relative(image_path: Path, output_path: Path) -> str:
    import os

    try:
        rel = os.path.relpath(image_path, output_path.parent)
    except ValueError:  # 跨盘符
        return image_path.as_posix()
    return Path(rel).as_posix()
