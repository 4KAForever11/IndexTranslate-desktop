"""批量工作台的统一数据模型：任何格式都先拆成扁平翻译单元。

解析器只负责「文件 ↔ 单元列表」的双向映射，翻译、排队、重试、断点续翻全部由
`app/core/batch/` 里的队列负责，因此新增格式（PDF / EPUB / DOCX）只需要再写一个
BaseDocumentParser 子类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ExportMode = Literal["monolingual", "bilingual"]

# 单元状态机
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

ALL_STATUSES = (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
)

STATUS_TEXT = {
    STATUS_PENDING: "待翻译",
    STATUS_RUNNING: "翻译中",
    STATUS_DONE: "已完成",
    STATUS_FAILED: "失败",
    STATUS_SKIPPED: "原样保留",
}


@dataclass
class TranslationUnit:
    """一个可独立翻译（或明确跳过）的最小单位。"""

    unit_index: int
    source_text: str = ""
    image_path: str | None = None
    context_hint: str = ""
    meta: dict = field(default_factory=dict)
    translated_text: str = ""
    status: str = STATUS_PENDING
    error_msg: str = ""
    warn: str = ""  # 占位符被模型改坏时的提示

    # ---------------------------------------------------------------- helpers
    @property
    def is_image(self) -> bool:
        return bool(self.image_path)

    @property
    def is_skipped(self) -> bool:
        return self.status == STATUS_SKIPPED

    @property
    def location(self) -> str:
        """给表格显示的定位文字：文件 + 行号 / 时间轴 / JSON 路径。"""
        where = str(self.meta.get("location") or "")
        if where:
            return where
        if self.is_image:
            return Path(str(self.image_path)).name
        return f"#{self.unit_index + 1}"

    @property
    def preview(self) -> str:
        text = self.source_text or (Path(str(self.image_path)).name if self.is_image else "")
        text = " ".join(text.split())
        return text[:80] + ("…" if len(text) > 80 else "")


def parse_failed_units(paths: list[Path], message: str) -> list[TranslationUnit]:
    """解析器整体失败时，用一个失败单元承载错误，界面不必特殊处理。"""
    names = "、".join(p.name for p in paths) or "（空）"
    return [
        TranslationUnit(
            unit_index=0,
            source_text="",
            status=STATUS_FAILED,
            error_msg=f"{message}（{names}）",
            meta={"location": names},
        )
    ]


class BaseDocumentParser(ABC):
    """可插拔解析器协议。"""

    parser_id: str = ""
    display_name: str = ""
    supported_exts: tuple[str, ...] = ()
    # 导出时可能额外写出的文件（例如 JSON 双语模式下的对照表）
    last_extras: list[Path]

    def __init__(self) -> None:
        self.last_extras = []

    # ------------------------------------------------------------------ hooks
    @classmethod
    def supports(cls, path: Path | str) -> bool:
        suffix = Path(path).suffix.lower()
        return suffix in cls.supported_exts

    @abstractmethod
    def parse(self, file_paths: list[Path]) -> list[TranslationUnit]:
        """把文件拆成标准翻译单元（跳过的块也要给出状态为 skipped 的单元）。"""

    @abstractmethod
    def export(
        self,
        original_paths: list[Path],
        units: list[TranslationUnit],
        output_path: Path,
        mode: ExportMode = "bilingual",
    ) -> Path:
        """按单语或双语对照模式重组并写出目标文件，返回实际写出的路径。"""

    # ----------------------------------------------------------------- shared
    @staticmethod
    def _by_index(units: list[TranslationUnit]) -> dict[int, TranslationUnit]:
        return {unit.unit_index: unit for unit in units}

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} {self.parser_id}>"
