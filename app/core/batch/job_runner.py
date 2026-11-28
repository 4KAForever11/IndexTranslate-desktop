"""批量任务执行器：串行消费单元，边翻边落库。

设计要点
- 每个单元完成后立刻写 SQLite，所以进程被杀掉后重启还能从断点继续；
- 图片不在解析阶段预读，直到真正消费该单元时才压缩成 Base64，然后立即释放；
- 走 `TranslationPipeline`，因此限流、缓存、429 降级对批量同样生效；
- 暂停 = 不再取下一个单元（当前单元会跑完），停止 = 取消当前请求并回到 pending。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from ..config import settings
from ..image_utils import prepare_image
from ..models import EngineError, TranslateRequest
from ..parsers import Guard
from ..parsers.base import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TranslationUnit,
)
from ..pipeline import get_pipeline
from .job_db import BatchJobDB

SUBTITLE_HINT = "这是视频字幕台词，译文要简短口语化、尽量一行说完，不要添加解释或换行。"


class BatchJobRunner(QThread):
    """单线程串行队列（并发交给底座限流器处理，这里不再叠加线程）。"""

    unitUpdated = pyqtSignal(str, int, str, str)  # job_id, unit_index, status, text
    jobProgress = pyqtSignal(str, int, int, int)  # job_id, done, failed, total
    jobStarted = pyqtSignal(str)
    jobFinished = pyqtSignal(str)
    jobFailed = pyqtSignal(str, str)
    notice = pyqtSignal(str)

    def __init__(self, db: BatchJobDB, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._cancel = threading.Event()
        self._current_job = ""

    # -------------------------------------------------------------- public API
    def enqueue(self, job_id: str) -> None:
        """把任务排进队列；线程会在后台自动开始处理。"""
        self._stop.clear()
        self._queue.put(job_id)
        if not self.isRunning():
            self.start()

    def pause(self) -> None:
        self._resume.clear()
        self.notice.emit("已暂停：当前单元会跑完，之后不再取新单元")

    def resume(self) -> None:
        self._resume.set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    @property
    def current_job(self) -> str:
        return self._current_job

    def stop(self, *, wait_ms: int = 5000) -> None:
        """取消当前请求并结束线程；未完成单元留在 pending，可继续。"""
        self._stop.set()
        self._cancel.set()
        self._resume.set()
        if self.isRunning():
            self.wait(wait_ms)

    def stop_soft(self) -> None:
        """只停止后续取号，线程退出，当前单元标记回 pending。"""
        self._stop.set()
        self._resume.set()

    # ------------------------------------------------------------------- thread
    def run(self) -> None:  # noqa: D401 - QThread entry
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                if not self._queue.empty():
                    continue
                break
            if self._stop.is_set():
                break
            self._process_job(job_id)
        self._current_job = ""

    # --------------------------------------------------------------------- jobs
    def _process_job(self, job_id: str) -> None:
        job = self._db.job(job_id)
        if not job:
            return
        pending = self._db.pending_units(job_id)
        if not pending:
            self._db.set_status(job_id, "done")
            self.jobFinished.emit(job_id)
            return

        self._current_job = job_id
        self._db.set_status(job_id, STATUS_RUNNING)
        self.jobStarted.emit(job_id)
        counts = self._db.counts(job_id)
        self.jobProgress.emit(job_id, counts["done"], counts["failed"], counts["total"])

        for unit in pending:
            if self._stop.is_set():
                break
            if not self._wait_while_paused():
                break
            self._translate_unit(job, unit)
            counts = self._db.counts(job_id)
            self.jobProgress.emit(job_id, counts["done"], counts["failed"], counts["total"])

        stopped = self._stop.is_set()
        self._db.set_status(job_id, STATUS_PENDING if stopped else "done")
        self.jobFinished.emit(job_id)
        self._current_job = ""

    def _wait_while_paused(self) -> bool:
        """暂停时在这里等待；返回 False 表示被要求退出。"""
        while not self._resume.is_set():
            if self._stop.is_set():
                return False
            time.sleep(0.05)
        return not self._stop.is_set()

    # -------------------------------------------------------------------- units
    def _translate_unit(self, job: dict[str, Any], unit: TranslationUnit) -> None:
        job_id = str(job["job_id"])
        index = int(unit.unit_index)
        self._db.update_unit(job_id, index, status=STATUS_RUNNING, error_msg="")
        self.unitUpdated.emit(job_id, index, STATUS_RUNNING, "")

        try:
            request = self._build_request(job, unit)
            pieces: list[str] = []
            for chunk in get_pipeline().execute_stream(request, self._cancel):
                if chunk.delta:
                    pieces.append(chunk.delta)
            text = "".join(pieces).strip()
            if not text:
                raise EngineError("服务没有返回译文，请稍后重试。")

            warn = ""
            mapping = unit.meta.get("mapping") or {}
            if mapping:
                text, missing = Guard.restore(text, mapping)
                if missing:
                    warn = f"占位符可能被改动：{'、'.join(missing)}"

            self._db.update_unit(
                job_id, index, status=STATUS_DONE, translated_text=text, warn=warn, error_msg=""
            )
            self.unitUpdated.emit(job_id, index, STATUS_DONE, text)
        except EngineError as exc:
            if self._cancel.is_set():
                self._db.update_unit(job_id, index, status=STATUS_PENDING, error_msg="")
                self.unitUpdated.emit(job_id, index, STATUS_PENDING, "")
                return
            self._db.update_unit(job_id, index, status=STATUS_FAILED, error_msg=exc.message)
            self.unitUpdated.emit(job_id, index, STATUS_FAILED, exc.message)
        except Exception as exc:  # 坏图 / 编码错误都不能拖垮整个队列
            self._db.update_unit(
                job_id, index, status=STATUS_FAILED, error_msg=f"{type(exc).__name__}: {exc}"
            )
            self.unitUpdated.emit(job_id, index, STATUS_FAILED, str(exc))

    def _build_request(self, job: dict[str, Any], unit: TranslationUnit) -> TranslateRequest:
        instruction = self._instruction_for(job, unit)
        image_b64 = None
        # 送翻的是「占位符保护后」的文本（source_text 留给展示与导出）。
        text = str(unit.meta.get("protected") or unit.source_text or "")
        if unit.is_image:
            image_b64 = self._image_payload(unit)
            text = unit.context_hint or ""
        return TranslateRequest(
            text=text,
            image_b64=image_b64,
            source_lang=str(job.get("source_lang") or "auto"),
            target_lang=str(job.get("target_lang") or "zh"),
            instruction=instruction,
            model=str(job.get("model") or settings.get("model") or ""),
            glossary_group=job.get("glossary_group"),
            use_cache=True,
            stream=False,
            scene="document",
            extra={"timeout": float(settings.get("timeout", 120) or 120)},
        )

    @staticmethod
    def _instruction_for(job: dict[str, Any], unit: TranslationUnit) -> str:
        parts: list[str] = []
        if job.get("parser_id") == "subtitle":
            parts.append(SUBTITLE_HINT)
        if unit.context_hint and not unit.is_image:
            parts.append(unit.context_hint)
        mapping = unit.meta.get("mapping") or {}
        if mapping:
            parts.append(Guard.hint_for(mapping))
        return "\n".join(p for p in parts if p).strip()

    @staticmethod
    def _image_payload(unit: TranslationUnit) -> str:
        path = Path(str(unit.image_path or ""))
        if not path.exists():
            raise EngineError(f"图片不存在：{path.name}")
        try:
            payload = prepare_image(str(path))
        except Exception as exc:
            raise EngineError(f"图片读取失败：{path.name}（{exc}）") from exc
        data_url = payload.data_url
        del payload  # 尽快释放内存
        if not data_url:
            raise EngineError(f"图片读取失败：{path.name}")
        return data_url


__all__ = ["BatchJobRunner", "SUBTITLE_HINT"]
