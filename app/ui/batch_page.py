"""批量翻译页：多张图片 / 多个文档排队翻译，逐项进度，失败可重试，一键导出。"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
)
from qfluentwidgets import (
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    TitleLabel,
)

from ..core import settings
from ..core.batch import (
    IMAGE_SUFFIXES,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_TEXT,
    TEXT_SUFFIXES,
    BatchItem,
    build_item,
    collect_paths,
    export_markdown,
)
from ..workers.translation_worker import TranslationWorker, build_request
from .theme import Card, MutedLabel, PageWidget

try:
    from qfluentwidgets import TableWidget
except ImportError:  # pragma: no cover
    from PyQt6.QtWidgets import QTableWidget as TableWidget  # type: ignore

FILTER = (
    "可翻译文件 (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.txt *.md *.markdown *.srt *.vtt);;"
    "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;"
    "文档 (*.txt *.md *.markdown *.srt *.vtt);;所有文件 (*.*)"
)

EXPORT_MODES = [("bilingual", "双语对照"), ("target", "仅译文"), ("both", "原文 + 译文分节")]

_ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_SUMMARY_CHARS = 46


class BatchRunner:
    """把每一项的每一段排进一个小并发池里跑。"""

    def __init__(self, page: "BatchPage") -> None:
        self.page = page
        self._pending: deque[tuple[int, int]] = deque()
        self._active: dict[TranslationWorker, tuple[int, int]] = {}
        self._started_at: dict[int, float] = {}
        self._stopping = False

    # ------------------------------------------------------------------ state
    @property
    def busy(self) -> bool:
        return bool(self._active) or bool(self._pending)

    @property
    def concurrency(self) -> int:
        try:
            return max(1, min(4, int(settings.get("batch_concurrency", 1) or 1)))
        except (TypeError, ValueError):
            return 1

    # ------------------------------------------------------------------ start
    def start(self, items: list[BatchItem]) -> None:
        self._pending.clear()
        self._stopping = False
        for index, item in enumerate(items):
            if item.status in {STATUS_DONE}:
                continue
            if item.status in {STATUS_FAILED, STATUS_CANCELLED} or item.finished:
                item.reset()
            item.status = STATUS_PENDING
            for segment_index in range(item.total):
                if item.results[segment_index] is None and not item.errors[segment_index]:
                    self._pending.append((index, segment_index))
        self.page.mark_running()
        self._pump()

    def stop(self) -> None:
        self._stopping = True
        self._pending.clear()
        for worker in list(self._active):
            worker.cancel()
        self.page.mark_idle()

    # ------------------------------------------------------------------- pool
    def _pump(self) -> None:
        while not self._stopping and self._pending and len(self._active) < self.concurrency:
            item_index, segment_index = self._pending.popleft()
            self._launch(item_index, segment_index)
        if not self._active and not self._pending:
            self.page.mark_idle()
            self.page.on_batch_finished()

    def _launch(self, item_index: int, segment_index: int) -> None:
        item = self.page.items[item_index]
        segment = item.segments[segment_index]
        item.status = STATUS_RUNNING
        self._started_at.setdefault(item_index, time.monotonic())

        request = build_request(
            text=segment.text,
            image_path=segment.image_path,
            kind="image" if segment.image_path else "text",
        )
        worker = TranslationWorker(request, self.page)
        worker.done.connect(
            lambda text, elapsed, i=item_index, s=segment_index, w=worker: self._on_done(i, s, w, text, elapsed)
        )
        worker.failed.connect(
            lambda message, detail, i=item_index, s=segment_index, w=worker: self._on_failed(i, s, w, message, detail)
        )
        worker.stopped.connect(
            lambda partial, i=item_index, s=segment_index, w=worker: self._on_stopped(i, s, w, partial)
        )
        worker.finished.connect(lambda w=worker: w.deleteLater())
        self._active[worker] = (item_index, segment_index)
        self.page.refresh_row(item_index)
        worker.start()

    # ---------------------------------------------------------------- results
    def _finish_segment(self, worker: TranslationWorker, item_index: int, segment_index: int) -> None:
        self._active.pop(worker, None)
        item = self.page.items[item_index]
        if item.finished >= item.total:
            item.elapsed_ms = int((time.monotonic() - self._started_at.get(item_index, time.monotonic())) * 1000)
            item.status = STATUS_DONE if any(r for r in item.results) else STATUS_FAILED
            self._started_at.pop(item_index, None)
        self.page.refresh_row(item_index)

    def _on_done(self, item_index: int, segment_index: int, worker, text: str, elapsed_ms: int) -> None:
        item = self.page.items[item_index]
        item.results[segment_index] = text
        self._finish_segment(worker, item_index, segment_index)
        self._pump()

    def _on_failed(self, item_index: int, segment_index: int, worker, message: str, detail: str) -> None:
        item = self.page.items[item_index]
        item.errors[segment_index] = message or "翻译失败"
        self._finish_segment(worker, item_index, segment_index)
        self._pump()

    def _on_stopped(self, item_index: int, segment_index: int, worker, partial: str) -> None:
        item = self.page.items[item_index]
        if partial:
            item.results[segment_index] = partial
        else:
            item.errors[segment_index] = "已取消"
        item.status = STATUS_CANCELLED if self._stopping else item.status
        self._finish_segment(worker, item_index, segment_index)
        self._pump()


class BatchPage(PageWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[BatchItem] = []
        self.runner = BatchRunner(self)
        self._build_ui()
        self._refresh_actions()

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(TitleLabel("批量翻译"))
        self.count_label = MutedLabel("队列为空", size=12)
        header.addWidget(self.count_label)
        header.addStretch(1)
        self.add_button = PushButton(FluentIcon.ADD, "添加文件")
        self.add_folder_button = PushButton(FluentIcon.FOLDER, "添加文件夹")
        self.clear_button = PushButton(FluentIcon.BROOM, "清空")
        header.addWidget(self.add_button)
        header.addWidget(self.add_folder_button)
        header.addWidget(self.clear_button)
        outer.addLayout(header)

        outer.addWidget(
            MutedLabel(
                "一次拖入多张截图（漫画 / PPT / 外文资料）或 .srt / .md / .txt 文档；"
                "按顺序排队翻译，失败的条目可以单独重试，最后合并导出为双语 Markdown。",
                size=12,
            )
        )

        self.table = TableWidget(self)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["文件", "状态", "进度", "用时", "摘要"])
        for column in range(5):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setTextAlignment(_ALIGN_LEFT)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        if hasattr(self.table, "setBorderRadius"):
            self.table.setBorderRadius(10)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        head = self.table.horizontalHeader()
        head.setFixedHeight(38)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 80)
        outer.addWidget(self.table, 1)

        self.empty_label = MutedLabel(
            "还没有文件。点右上角「添加文件」，或把图片 / 文档直接拖进这个窗口。", size=12
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.empty_label)

        tools = Card(radius=8)
        tools_layout = QHBoxLayout(tools)
        tools_layout.setContentsMargins(14, 10, 14, 10)
        tools_layout.setSpacing(10)
        tools_layout.addWidget(MutedLabel("并发", size=12))
        self.concurrency_spin = SpinBox()
        self.concurrency_spin.setRange(1, 4)
        # 88 px 会把数字挤成 0 宽（qfluentwidgets 的 SpinBox sizeHint ≈ 106）
        self.concurrency_spin.setFixedWidth(120)
        self.concurrency_spin.setToolTip("同时进行的请求数；公网免费服务建议保持 1–2")
        self.concurrency_spin.setValue(int(settings.get("batch_concurrency", 1) or 1))
        tools_layout.addWidget(self.concurrency_spin)

        tools_layout.addSpacing(12)
        tools_layout.addWidget(MutedLabel("导出格式", size=12))
        self.export_combo = ComboBox()
        self.export_combo.setMinimumWidth(120)
        for code, label in EXPORT_MODES:
            self.export_combo.addItem(label, userData=code)
        index = self.export_combo.findData(settings.get("batch_export", "bilingual"))
        self.export_combo.setCurrentIndex(max(0, index))
        tools_layout.addWidget(self.export_combo)
        self.hint_label = MutedLabel("", size=12)
        tools_layout.addSpacing(8)
        tools_layout.addWidget(self.hint_label)
        tools_layout.addStretch(1)

        self.retry_button = PushButton(FluentIcon.SYNC, "重试失败")
        self.start_button = PrimaryPushButton("开始翻译")
        self.start_button.setMinimumWidth(120)
        self.stop_button = PushButton("停止")
        self.stop_button.setEnabled(False)
        self.export_button = PushButton(FluentIcon.SAVE, "导出")
        for widget in (self.retry_button, self.start_button, self.stop_button, self.export_button):
            tools_layout.addWidget(widget)
        outer.addWidget(tools)

        self.add_button.clicked.connect(self.pick_files)
        self.add_folder_button.clicked.connect(self.pick_folder)
        self.clear_button.clicked.connect(self.clear)
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.retry_button.clicked.connect(self.retry_failed)
        self.export_button.clicked.connect(self.export)
        self.concurrency_spin.valueChanged.connect(self._on_concurrency_changed)
        self.export_combo.currentIndexChanged.connect(self._on_export_changed)

    # ------------------------------------------------------------------ queue
    def add_paths(self, paths) -> int:
        """加入队列；返回真正新增的文件数。"""
        known = {str(item.path.resolve()).lower() for item in self.items}
        added = 0
        for path in collect_paths(paths):
            key = str(path.resolve()).lower()
            if key in known:
                continue
            try:
                item = build_item(path)
            except OSError as exc:
                self._error(f"无法读取 {path.name}：{exc}")
                continue
            if not item.segments:
                self._error(f"{path.name} 里没有可翻译的内容。")
                continue
            known.add(key)
            self.items.append(item)
            added += 1
        if added:
            self._render()
            self.changed.emit()
        return added

    def pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要翻译的文件", settings.get("batch_last_dir", "") or "", FILTER
        )
        if not paths:
            return
        settings.set("batch_last_dir", str(Path(paths[0]).parent))
        settings.save()
        self.add_paths(paths)

    def pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "选择文件夹", settings.get("batch_last_dir", "") or ""
        )
        if not folder:
            return
        settings.set("batch_last_dir", folder)
        settings.save()
        added = self.add_paths([folder])
        if not added:
            self._warn("没有可翻译的文件", "该文件夹里没有受支持的图片或文档。")

    def clear(self) -> None:
        if self.runner.busy:
            self._warn("正在翻译", "请先点「停止」再清空队列。")
            return
        if not self.items:
            return
        self.items.clear()
        self._render()
        self._set_hint("")
        self.changed.emit()

    # ------------------------------------------------------------------ run
    def start(self) -> None:
        if self.runner.busy:
            return
        if not self.items:
            self._warn("队列为空", "先添加要翻译的图片或文档。")
            return
        if not any(item.status != STATUS_DONE for item in self.items):
            self._warn("已经全部完成", "如需重新翻译，先点「重试失败」或清空队列。")
            return
        self._set_hint("")
        self.runner.start(self.items)

    def stop(self) -> None:
        self.runner.stop()

    def retry_failed(self) -> None:
        if self.runner.busy:
            return
        pending = [item for item in self.items if item.status == STATUS_FAILED]
        if not pending:
            self._warn("没有失败的条目", "当前队列里所有条目都已完成。")
            return
        for item in pending:
            item.reset()
        self._render()
        self._set_hint("")
        self.runner.start(self.items)

    def mark_running(self) -> None:
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.retry_button.setEnabled(False)

    def mark_idle(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.retry_button.setEnabled(True)
        self._refresh_actions()

    def on_batch_finished(self) -> None:
        done = sum(1 for item in self.items if item.status == STATUS_DONE)
        if done:
            self._set_hint(f"已完成 {done}/{len(self.items)} · 可导出")
        self._refresh_actions()

    # ----------------------------------------------------------------- render
    def _render(self) -> None:
        self.table.setRowCount(len(self.items))
        for index, item in enumerate(self.items):
            self._fill_row(index)
        self.empty_label.setVisible(not self.items)
        self.table.setVisible(bool(self.items))
        self._refresh_actions()

    def _fill_row(self, index: int) -> None:
        item = self.items[index]
        name_item = QTableWidgetItem(item.name)
        name_item.setToolTip(str(item.path))
        name_item.setTextAlignment(_ALIGN_LEFT)
        self.table.setItem(index, 0, name_item)
        self.table.setItem(index, 1, QTableWidgetItem(STATUS_TEXT.get(item.status, item.status)))
        self.table.setItem(index, 2, QTableWidgetItem(f"{item.finished}/{item.total}"))
        self.table.setItem(
            index, 3, QTableWidgetItem(f"{item.elapsed_ms / 1000:.1f}s" if item.elapsed_ms else "—")
        )
        summary = item.summary
        if len(summary) > _SUMMARY_CHARS:
            summary = summary[:_SUMMARY_CHARS - 1].rstrip() + "…"
        summary_item = QTableWidgetItem(summary)
        summary_item.setToolTip(item.summary)
        self.table.setItem(index, 4, summary_item)

    def refresh_row(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self._fill_row(index)
        done = sum(1 for item in self.items if item.status == STATUS_DONE)
        self.count_label.setText(f"共 {len(self.items)} 个 · 完成 {done} 个")

    def _refresh_actions(self) -> None:
        has_items = bool(self.items)
        self.clear_button.setEnabled(has_items)
        self.export_button.setEnabled(
            any(item.status == STATUS_DONE for item in self.items)
        )
        self.start_button.setEnabled(has_items and not self.runner.busy)
        if not self.runner.busy:
            done = sum(1 for item in self.items if item.status == STATUS_DONE)
            self.count_label.setText(
                f"共 {len(self.items)} 个 · 完成 {done} 个" if has_items else "队列为空"
            )

    # ------------------------------------------------------------------ export
    def export(self) -> None:
        finished = [item for item in self.items if item.status == STATUS_DONE]
        if not finished:
            self._warn("没有可导出的内容", "先翻译至少一个文件。")
            return
        mode = str(self.export_combo.currentData() or "bilingual")
        content = export_markdown(self.items, mode)
        default = str(Path(settings.get("batch_last_dir", "") or Path.home()) / "批量翻译.md")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出翻译结果", default, "Markdown (*.md);;文本文件 (*.txt)"
        )
        if not path:
            return
        try:
            Path(path).write_text(content, encoding="utf-8")
        except OSError as exc:
            self._error(f"写入失败：{exc}")
            return
        self._set_hint(f"已导出 {Path(path).name}")

    # ------------------------------------------------------------------ misc
    def _set_hint(self, text: str) -> None:
        self.hint_label.setText(text)

    def _on_concurrency_changed(self, value: int) -> None:
        settings.set("batch_concurrency", int(value))
        settings.save()

    def _on_export_changed(self, _index: int) -> None:
        settings.set("batch_export", str(self.export_combo.currentData() or "bilingual"))
        settings.save()

    def _warn(self, title: str, content: str) -> None:
        InfoBar.warning(
            title=title,
            content=content,
            duration=2600,
            position=InfoBarPosition.BOTTOM_RIGHT,
            parent=self,
        )

    def _error(self, message: str) -> None:
        InfoBar.error(
            title="操作失败",
            content=message,
            duration=4000,
            position=InfoBarPosition.BOTTOM_RIGHT,
            parent=self,
        )

    def shutdown(self) -> None:
        if self.runner.busy:
            self.runner.stop()
