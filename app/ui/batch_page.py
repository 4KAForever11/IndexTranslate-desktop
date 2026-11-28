"""批量工作台：统一任务队列 + 可插拔解析器。

三块区域：
- 左侧任务列表（每个文件 / 每批图片 = 一个任务，带进度与成功失败数，重启后自动恢复）
- 右上工具栏（源/目标语言、模型、术语分组、输出模式、开始 / 暂停 / 导出）
- 右下单元表（位置 / 原文 / 译文 / 状态，可筛选、可双击改译文、可单独重试）
"""

from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    TitleLabel,
    TogglePushButton,
)

from ..core import settings
from ..core.batch import BatchJobDB, BatchJobRunner
from ..core.config import MODEL_IDS, data_dir, model_label
from ..core.glossary_db import DEFAULT_GROUP
from ..core.languages import source_languages, target_languages
from ..core.parsers import (
    STATUS_TEXT,
    parser_by_id,
    parser_for,
    group_paths,
    supported_extensions,
)
from ..core.parsers.base import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    TranslationUnit,
)
from .theme import Card, Divider, MutedLabel, PageWidget, TextLabel, colors

EXPORT_MODES = [("bilingual", "双语对照"), ("monolingual", "仅译文")]
FILTERS = [("all", "全部"), ("failed", "失败"), ("warn", "占位符告警")]
_ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_FILE_FILTER = "可翻译文件 (" + " ".join(f"*{ext}" for ext in supported_extensions()) + ");;所有文件 (*.*)"


class JobCard(Card):
    """左侧任务卡片。"""

    clicked = pyqtSignal(str)

    def __init__(self, job: dict, counts: dict, parent=None) -> None:
        super().__init__(parent, radius=8)
        self.job_id = str(job["job_id"])
        self._selected = False
        self._counts = counts
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 10)
        layout.setSpacing(3)

        self.title_label = TextLabel(str(job.get("title") or "未命名"), size=13, bold=True)
        self.title_label.setWordWrap(False)
        layout.addWidget(self.title_label)

        parser = parser_by_id(str(job.get("parser_id") or ""))
        parser_name = parser.display_name if parser else str(job.get("parser_id") or "")
        self.meta_label = MutedLabel(f"{parser_name} · {job.get('source_lang')} → {job.get('target_lang')}", size=11)
        layout.addWidget(self.meta_label)

        self.progress_label = MutedLabel("", size=11)
        layout.addWidget(self.progress_label)
        self.set_counts(counts)

    def set_counts(self, counts: dict) -> None:
        self._counts = counts
        total = int(counts.get("total") or 0)
        done = int(counts.get("done") or 0)
        failed = int(counts.get("failed") or 0)
        text = f"{done}/{total}"
        if failed:
            text += f" · 失败 {failed}"
        self.progress_label.setText(text)
        self.progress_label.setStyleSheet(
            f"color: {colors()['danger']};" if failed else ""
        )

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if not self._selected:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors()["accent"]))
        painter.drawRoundedRect(0, 8, 3, self.height() - 16, 1.5, 1.5)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.job_id)
        super().mousePressEvent(event)


class BatchPage(PageWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.db = BatchJobDB(Path(data_dir()) / "jobs.db")
        self.runner = BatchJobRunner(self.db, self)
        self._cards: dict[str, JobCard] = {}
        self._jobs: dict[str, dict] = {}
        self._counts: dict[str, dict] = {}
        self._selected = ""
        self._filter = "all"
        self._units: list[TranslationUnit] = []
        self._loading = False
        # 已排入队列但还没跑完的任务数：runner.current_job 只有在真正开跑后才有值，
        # 用它判断"忙碌"会让「暂停」在排队阶段一直是灰的。
        self._queued = 0
        self._build_ui()
        self._connect_runner()
        self.setAcceptDrops(True)
        self.reload_jobs()

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(TitleLabel("批量工作台"))
        self.count_label = MutedLabel("队列为空", size=12)
        header.addWidget(self.count_label)
        header.addStretch(1)
        self.add_button = PushButton(FluentIcon.ADD, "添加文件")
        self.add_folder_button = PushButton(FluentIcon.FOLDER, "添加文件夹")
        self.clear_button = PushButton(FluentIcon.BROOM, "清空")
        for button in (self.add_button, self.add_folder_button, self.clear_button):
            header.addWidget(button)
        outer.addLayout(header)

        subtitle = MutedLabel(
            "拖入多张截图或 .srt / .md / .txt / .json 文档：每个文件（或一批图片）成为一个任务，"
            "边翻边存盘，中途退出也能接着翻；代码块与占位符会原样保留。",
            size=12,
        )
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.toolbar = self._build_toolbar()
        outer.addWidget(self.toolbar)

        body = QHBoxLayout()
        body.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(MutedLabel("任务", size=12))
        self.job_area = SmoothScrollArea()
        self.job_area.setWidgetResizable(True)
        self.job_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.job_area.setFrameShape(SmoothScrollArea.Shape.NoFrame)
        self.job_area.setStyleSheet("SmoothScrollArea { background: transparent; }")
        self.job_area.setFixedWidth(240)
        self.job_host = QWidget()
        self.job_host.setStyleSheet("background: transparent;")
        self.job_layout = QVBoxLayout(self.job_host)
        self.job_layout.setContentsMargins(0, 0, 6, 0)
        self.job_layout.setSpacing(8)
        self.job_layout.addStretch(1)
        self.job_area.setWidget(self.job_host)
        left.addWidget(self.job_area, 1)
        self.empty_jobs = MutedLabel("还没有任务，\n先添加或拖入文件。", size=12)
        self.empty_jobs.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left.addWidget(self.empty_jobs)
        body.addLayout(left)

        right = QVBoxLayout()
        right.setSpacing(8)
        filters = QHBoxLayout()
        filters.setSpacing(6)
        self.filter_buttons: dict[str, TogglePushButton] = {}
        for key, label in FILTERS:
            button = TogglePushButton(label)
            button.setFixedHeight(28)
            button.clicked.connect(lambda _checked=False, k=key: self._set_filter(k))
            self.filter_buttons[key] = button
            filters.addWidget(button)
        self.filter_buttons["all"].setChecked(True)
        filters.addStretch(1)
        self.unit_state = MutedLabel("", size=11)
        filters.addWidget(self.unit_state)
        right.addLayout(filters)

        self.table = self._build_table()
        right.addWidget(self.table, 1)
        body.addLayout(right, 1)
        outer.addLayout(body, 1)

    def _build_toolbar(self) -> Card:
        """两行布局：窄窗口（默认 1180、最小 960）下也不会互相挤压。"""
        card = Card(radius=8)
        outer = QVBoxLayout(card)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(MutedLabel("源语言", size=12))
        self.src_combo = self._combo(128)
        for code, name in source_languages():
            self.src_combo.addItem(name, userData=code)
        row.addWidget(self.src_combo)

        row.addWidget(MutedLabel("目标语言", size=12))
        self.tgt_combo = self._combo(128)
        for code, name in target_languages():
            self.tgt_combo.addItem(name, userData=code)
        row.addWidget(self.tgt_combo)

        row.addWidget(MutedLabel("模型", size=12))
        self.model_combo = self._combo(110)
        for model_id in MODEL_IDS:
            self.model_combo.addItem(model_label(model_id), userData=model_id)
        row.addWidget(self.model_combo)
        row.addStretch(1)
        outer.addLayout(row)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addWidget(MutedLabel("术语", size=12))
        self.glossary_combo = self._combo(108)
        self.glossary_combo.addItem("不使用", userData=None)
        self.glossary_combo.addItem("全部术语", userData="*")
        actions.addWidget(self.glossary_combo)

        actions.addWidget(MutedLabel("输出", size=12))
        self.export_combo = self._combo(104)
        for code, label in EXPORT_MODES:
            self.export_combo.addItem(label, userData=code)
        actions.addWidget(self.export_combo)

        hint = MutedLabel("失败只影响单个单元，可单独重试。", size=11)
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hint.setToolTip("每个文件（或一批图片）独立成任务；失败的单元可以只重试它自己。")
        self.toolbar_hint = hint
        actions.addWidget(hint, 1)
        self.start_button = PrimaryPushButton("开始翻译")
        self.start_button.setFixedWidth(112)
        self.pause_button = PushButton("暂停")
        self.pause_button.setFixedWidth(72)
        self.pause_button.setEnabled(False)
        self.retry_button = PushButton(FluentIcon.SYNC, "重试失败")
        self.retry_button.setFixedWidth(104)
        self.export_button = PushButton(FluentIcon.SAVE, "导出")
        self.export_button.setFixedWidth(88)
        for button in (self.start_button, self.pause_button, self.retry_button, self.export_button):
            actions.addWidget(button)
        outer.addLayout(actions)

        self.add_button.clicked.connect(self.pick_files)
        self.add_folder_button.clicked.connect(self.pick_folder)
        self.clear_button.clicked.connect(self.clear)
        self.start_button.clicked.connect(self.start)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.retry_button.clicked.connect(self.retry_failed)
        self.export_button.clicked.connect(self.export)
        self.src_combo.currentIndexChanged.connect(self._on_lang_changed)
        self.tgt_combo.currentIndexChanged.connect(self._on_lang_changed)
        self.model_combo.currentIndexChanged.connect(self._on_lang_changed)
        self._load_choices()
        return card

    @staticmethod
    def _combo(width: int) -> ComboBox:
        combo = ComboBox()
        combo.setFixedWidth(width)
        return combo

    def _build_table(self):
        try:
            from qfluentwidgets import TableWidget as _Table
        except ImportError:  # pragma: no cover
            from PyQt6.QtWidgets import QTableWidget as _Table  # type: ignore

        table = _Table(self)
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["位置", "原文 / 图片", "译文", "状态"])
        for column in range(4):
            item = table.horizontalHeaderItem(column)
            if item is not None:
                item.setTextAlignment(_ALIGN_LEFT)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        if hasattr(table, "setBorderRadius"):
            table.setBorderRadius(10)
        table.setWordWrap(False)
        table.setAlternatingRowColors(False)
        table.verticalHeader().setDefaultSectionSize(38)
        head = table.horizontalHeader()
        head.setFixedHeight(38)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(0, 170)
        table.setColumnWidth(3, 92)
        table.itemChanged.connect(self._on_item_changed)
        return table

    # ------------------------------------------------------------------ choices
    def _load_choices(self) -> None:
        self._select_data(self.src_combo, str(settings.get("source_lang", "auto")))
        self._select_data(self.tgt_combo, str(settings.get("target_lang", "zh")))
        self._select_data(self.model_combo, str(settings.get("model", "")))
        self._select_data(self.export_combo, str(settings.get("batch_export", "bilingual")))
        group = settings.get("glossary_group", DEFAULT_GROUP)
        if self.glossary_combo.findData(group) < 0 and group:
            self.glossary_combo.addItem(str(group), userData=group)
        self._select_data(self.glossary_combo, group if settings.get("use_glossary", True) else None)

    def reload_glossary_groups(self, groups: list[str]) -> None:
        current = self.glossary_combo.currentData()
        self.glossary_combo.blockSignals(True)
        self.glossary_combo.clear()
        self.glossary_combo.addItem("不使用", userData=None)
        self.glossary_combo.addItem("全部术语", userData="*")
        for name in groups:
            self.glossary_combo.addItem(str(name), userData=str(name))
        self.glossary_combo.blockSignals(False)
        self._select_data(self.glossary_combo, current)

    @staticmethod
    def _select_data(combo: ComboBox, value) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _on_lang_changed(self, _index: int = 0) -> None:
        settings.set("batch_export", str(self.export_combo.currentData() or "bilingual"))
        settings.save()

    # -------------------------------------------------------------------- jobs
    def reload_jobs(self) -> None:
        self._jobs = {str(job["job_id"]): job for job in self.db.jobs()}
        self._counts = {job_id: self.db.counts(job_id) for job_id in self._jobs}
        if self._selected not in self._jobs:
            self._selected = next(iter(self._jobs), "")
        self._render_jobs()
        self._load_units()
        self._refresh_actions()

    def _render_jobs(self) -> None:
        while self.job_layout.count() > 1:
            item = self.job_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._cards.clear()
        for job_id, job in self._jobs.items():
            card = JobCard(job, self._counts.get(job_id, {}), self.job_host)
            card.clicked.connect(self.select_job)
            card.set_selected(job_id == self._selected)
            self.job_layout.insertWidget(self.job_layout.count() - 1, card)
            self._cards[job_id] = card
        self.empty_jobs.setVisible(not self._jobs)
        self.job_area.setVisible(bool(self._jobs))
        self._update_count_label()

    def _update_count_label(self) -> None:
        total = len(self._jobs)
        if not total:
            self.count_label.setText("队列为空")
            return
        unfinished = sum(
            1
            for job_id in self._jobs
            if self._counts.get(job_id, {}).get("total", 0)
            > self._counts.get(job_id, {}).get("done", 0)
        )
        self.count_label.setText(f"共 {total} 个任务 · {unfinished} 个未完成")

    def select_job(self, job_id: str) -> None:
        if job_id not in self._jobs:
            return
        self._selected = job_id
        for key, card in self._cards.items():
            card.set_selected(key == job_id)
        self._load_units()
        self._refresh_actions()

    def add_paths(self, paths) -> int:
        files: list[Path] = []
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                try:
                    files.extend(sorted(p for p in path.iterdir() if p.is_file()))
                except OSError as exc:
                    self._error(f"无法读取文件夹 {path.name}：{exc}")
            elif path.is_file():
                files.append(path)
        groups = group_paths(files)
        if not groups:
            self._warn("没有可翻译的文件", "支持的类型：" + " ".join(supported_extensions()))
            return 0

        added = 0
        for parser, group_files in groups:
            title = group_files[0].name if len(group_files) == 1 else f"{len(group_files)} 张图片"
            try:
                units = parser.parse(group_files)
            except Exception as exc:  # 解析失败也要留痕，而不是静默丢掉
                self._error(f"{title} 解析失败：{exc}")
                continue
            if not units:
                continue
            job_id = self.db.create_job(
                title=title,
                parser_id=parser.parser_id,
                files=group_files,
                source_lang=str(self.src_combo.currentData() or "auto"),
                target_lang=str(self.tgt_combo.currentData() or "zh"),
                model=str(self.model_combo.currentData() or ""),
                glossary_group=self.glossary_combo.currentData(),
                export_mode=str(self.export_combo.currentData() or "bilingual"),
            )
            self.db.add_units(job_id, units)
            added += 1
            self._selected = job_id

        if added:
            self.reload_jobs()
            self.changed.emit()
        return added

    def pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要翻译的文件", str(settings.get("batch_last_dir", "") or ""), _FILE_FILTER
        )
        if not paths:
            return
        settings.set("batch_last_dir", str(Path(paths[0]).parent))
        settings.save()
        self.add_paths(paths)

    def pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "选择文件夹", str(settings.get("batch_last_dir", "") or "")
        )
        if not folder:
            return
        settings.set("batch_last_dir", folder)
        settings.save()
        if not self.add_paths([folder]):
            self._warn("没有可翻译的文件", "该文件夹里没有受支持的文件。")

    def clear(self) -> None:
        if self.runner.current_job:
            self._warn("正在翻译", "请先点「暂停」再清空。")
            return
        if not self._jobs:
            return
        for job_id in list(self._jobs):
            self.db.delete_job(job_id)
        self._selected = ""
        self._queued = 0
        self.reload_jobs()
        self.changed.emit()

    # --------------------------------------------------------------------- run
    def start(self) -> None:
        if not self._jobs:
            self._warn("队列为空", "先添加要翻译的文件。")
            return
        queued = 0
        for job_id, job in self._jobs.items():
            pending = self.db.pending_units(job_id)
            if not pending:
                continue
            self.db.set_status(job_id, STATUS_PENDING)
            self.runner.enqueue(job_id)
            queued += 1
        if not queued:
            self._warn("已经全部完成", "如需重翻，先点「重试失败」，或清空后重新添加。")
            return
        self._queued += queued
        self._refresh_actions()
        self.unit_state.setText(f"已排入 {queued} 个任务")

    def toggle_pause(self) -> None:
        if self.runner.paused:
            self.runner.resume()
            self.pause_button.setText("暂停")
            self.unit_state.setText("已继续")
        else:
            self.runner.pause()
            self.pause_button.setText("继续")
            self.unit_state.setText("已暂停（当前单元会跑完）")

    def retry_failed(self) -> None:
        target = self._selected or ""
        if not target:
            self._warn("没有选中的任务", "先在左边选一个任务。")
            return
        count = self.db.reset_failed(target)
        if not count:
            self._warn("没有失败的单元", "当前任务没有失败条目。")
            return
        self.db.set_status(target, STATUS_PENDING)
        self.runner.enqueue(target)
        self._queued += 1
        self._refresh_actions()
        self._load_units()

    # ------------------------------------------------------------------ runner
    def _connect_runner(self) -> None:
        self.runner.unitUpdated.connect(self._on_unit_updated)
        self.runner.jobProgress.connect(self._on_job_progress)
        self.runner.jobFinished.connect(self._on_job_finished)
        self.runner.notice.connect(self.unit_state.setText)

    def _on_unit_updated(self, job_id: str, index: int, status: str, text: str) -> None:
        if job_id != self._selected:
            return
        for row, unit in enumerate(self._units):
            if unit.unit_index != index:
                continue
            unit.status = status
            unit.translated_text = text if status == STATUS_DONE else unit.translated_text
            if status == STATUS_FAILED:
                unit.error_msg = text
            self._fill_row(row, unit)
            break
        self._update_unit_state()

    def _on_job_progress(self, job_id: str, done: int, failed: int, total: int) -> None:
        self._counts[job_id] = {
            **self._counts.get(job_id, {}),
            "total": total,
            "done": done,
            "failed": failed,
        }
        card = self._cards.get(job_id)
        if card is not None:
            card.set_counts(self._counts[job_id])
        self._update_count_label()
        self._update_unit_state()

    def _on_job_finished(self, job_id: str) -> None:
        self._queued = max(0, self._queued - 1)
        job = self.db.job(job_id)
        if job:
            self._jobs[job_id] = job
        self._counts[job_id] = self.db.counts(job_id)
        card = self._cards.get(job_id)
        if card is not None:
            card.set_counts(self._counts[job_id])
        if job_id == self._selected:
            self._load_units()
        self._update_count_label()
        self._refresh_actions()

    # ------------------------------------------------------------------- units
    def _load_units(self) -> None:
        self._units = self.db.units(self._selected) if self._selected else []
        self._render_units()

    def _visible_units(self) -> list[TranslationUnit]:
        if self._filter == "failed":
            return [u for u in self._units if u.status == STATUS_FAILED]
        if self._filter == "warn":
            return [u for u in self._units if u.warn]
        return self._units

    def _render_units(self) -> None:
        visible = self._visible_units()
        self._loading = True
        self.table.setRowCount(len(visible))
        for row, unit in enumerate(visible):
            self._fill_row(row, unit)
        self._loading = False
        self._update_unit_state()

    def _fill_row(self, row: int, unit: TranslationUnit) -> None:
        block = self.table.blockSignals(True)
        try:
            location = QTableWidgetItem(unit.location)
            location.setTextAlignment(_ALIGN_LEFT)
            location.setToolTip(unit.location)
            self.table.setItem(row, 0, location)

            source = QTableWidgetItem(unit.preview if not unit.is_image else f"🖼 {Path(str(unit.image_path)).name}")
            source.setTextAlignment(_ALIGN_LEFT)
            source.setToolTip(unit.source_text or str(unit.image_path or ""))
            source.setFlags(source.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 1, source)

            translated = QTableWidgetItem(unit.translated_text or "")
            translated.setTextAlignment(_ALIGN_LEFT)
            translated.setToolTip("双击可以直接修改译文")
            self.table.setItem(row, 2, translated)

            label = STATUS_TEXT.get(unit.status, unit.status)
            if unit.warn:
                label += " · 需复核"
            status_item = QTableWidgetItem(label)
            status_item.setTextAlignment(_ALIGN_LEFT)
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if unit.status == STATUS_FAILED:
                status_item.setToolTip(unit.error_msg)
            elif unit.warn:
                status_item.setToolTip(unit.warn)
            self.table.setItem(row, 3, status_item)
        finally:
            self.table.blockSignals(block)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 2 or not self._selected:
            return
        visible = self._visible_units()
        if item.row() >= len(visible):
            return
        unit = visible[item.row()]
        text = item.text().strip()
        if text == unit.translated_text:
            return
        self.db.update_unit(
            self._selected,
            unit.unit_index,
            translated_text=text,
            status=STATUS_DONE if text else STATUS_PENDING,
        )
        unit.translated_text = text
        self._counts[self._selected] = self.db.counts(self._selected)
        card = self._cards.get(self._selected)
        if card is not None:
            card.set_counts(self._counts[self._selected])
        self._update_unit_state()

    def _set_filter(self, key: str) -> None:
        self._filter = key
        for name, button in self.filter_buttons.items():
            button.setChecked(name == key)
        self._render_units()

    def _update_unit_state(self) -> None:
        if not self._units:
            self.unit_state.setText("")
            return
        done = sum(1 for u in self._units if u.status == STATUS_DONE)
        failed = sum(1 for u in self._units if u.status == STATUS_FAILED)
        warn = sum(1 for u in self._units if u.warn)
        text = f"{done}/{len(self._units)} 已完成"
        if failed:
            text += f" · {failed} 失败"
        if warn:
            text += f" · {warn} 需复核"
        self.unit_state.setText(text)

    # ------------------------------------------------------------------ export
    def export(self) -> None:
        if not self._selected:
            self._warn("没有选中的任务", "先在左边选一个任务。")
            return
        job = self._jobs.get(self._selected)
        if not job:
            return
        parser = parser_by_id(str(job.get("parser_id") or ""))
        if parser is None:
            self._error("找不到对应的解析器，无法导出。")
            return
        units = self.db.units(self._selected)
        if not any(u.status == STATUS_DONE for u in units):
            self._warn("没有可导出的内容", "先翻译至少一个单元。")
            return

        mode = str(self.export_combo.currentData() or "bilingual")
        files = [Path(f) for f in job.get("files") or []]
        suffix = files[0].suffix if files else ".md"
        default_dir = Path(settings.get("batch_last_dir", "") or Path.home())
        default = str(default_dir / f"{Path(str(job.get('title') or '导出')).stem}.{mode}{suffix}")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出翻译结果", default, "所有文件 (*.*)"
        )
        if not path:
            return
        try:
            out = parser.export(files, units, Path(path), mode)
            extras = list(getattr(parser, "last_extras", []) or [])
        except Exception as exc:
            self._error(f"导出失败：{exc}")
            return
        message = f"已导出 {out.name}"
        if extras:
            message += f" + {extras[0].name}"
        self.unit_state.setText(message)

    # ------------------------------------------------------------------- misc
    def _refresh_actions(self) -> None:
        has_jobs = bool(self._jobs)
        busy = self._queued > 0 or bool(self.runner.current_job)
        self.clear_button.setEnabled(has_jobs and not busy)
        self.start_button.setEnabled(has_jobs and not busy)
        self.pause_button.setEnabled(busy)
        if not busy and self.pause_button.text() != "暂停":
            self.pause_button.setText("暂停")
        self.retry_button.setEnabled(bool(self._selected))
        self.export_button.setEnabled(bool(self._selected))

    def _warn(self, title: str, content: str) -> None:
        InfoBar.warning(
            title=title, content=content, duration=2800, position=InfoBarPosition.BOTTOM_RIGHT, parent=self
        )

    def _error(self, message: str) -> None:
        InfoBar.error(
            title="操作失败", content=message, duration=4200, position=InfoBarPosition.BOTTOM_RIGHT, parent=self
        )

    # -------------------------------------------------------------------- drag
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            added = self.add_paths(paths)
            if added:
                self.unit_state.setText(f"已加入 {added} 个任务")
        else:
            super().dropEvent(event)

    def shutdown(self) -> None:
        if self.runner.isRunning():
            self.runner.stop(wait_ms=4000)
        self.db.close()
