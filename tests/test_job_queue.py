"""批量任务队列：持久化、断点续翻、暂停/停止、失败重试。

测试用假的 pipeline 顶替真实请求，不联网。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from app.core.batch import BatchJobDB, BatchJobRunner
from app.core.batch import job_runner as runner_module
from app.core.models import EngineError, TranslateChunk
from app.core.parsers.base import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    TranslationUnit,
)

_APP = QCoreApplication.instance() or QCoreApplication([])


class FakePipeline:
    """按调用顺序记录请求，并可让指定次序的调用失败。"""

    def __init__(self, fail_on: set[int] | None = None, delay: float = 0.0) -> None:
        self.calls: list = []
        self.fail_on = fail_on or set()
        self.delay = delay

    def execute_stream(self, request, cancel_event=None):
        self.calls.append(request)
        if self.delay:
            time.sleep(self.delay)
        order = len(self.calls)
        if order in self.fail_on:
            raise EngineError("模拟失败")
        text = request.text or "IMG"
        yield TranslateChunk(delta=f"T{order}:{text}")
        yield TranslateChunk(delta="", done=True, model_used=request.model)


def _units(count: int) -> list[TranslationUnit]:
    return [
        TranslationUnit(unit_index=i, source_text=f"line {i + 1}", meta={"location": f"#{i + 1}"})
        for i in range(count)
    ]


def _pump(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class JobDBTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "jobs.db"
        self.db = BatchJobDB(self.path)
        self.addCleanup(self.db.close)

    def _job(self, units: list[TranslationUnit] | None = None) -> str:
        job_id = self.db.create_job(
            title="测试任务",
            parser_id="markdown",
            files=["a.md"],
            model="Index-Translate-9B",
            glossary_group="默认",
        )
        self.db.add_units(job_id, units if units is not None else _units(3))
        return job_id

    def test_create_and_counts(self) -> None:
        job_id = self._job()
        job = self.db.job(job_id)
        self.assertEqual(job["title"], "测试任务")
        self.assertEqual(job["files"], ["a.md"])
        self.assertEqual(job["model"], "Index-Translate-9B")
        counts = self.db.counts(job_id)
        self.assertEqual((counts["total"], counts["done"], counts["failed"]), (3, 0, 0))

    def test_unit_update_survives_reopen(self) -> None:
        job_id = self._job()
        self.db.update_unit(job_id, 1, status=STATUS_DONE, translated_text="译 2")
        self.db.close()
        self.db = BatchJobDB(self.path)  # 模拟重启
        units = self.db.units(job_id)
        self.assertEqual(units[1].status, STATUS_DONE)
        self.assertEqual(units[1].translated_text, "译 2")
        self.assertEqual([u.unit_index for u in self.db.pending_units(job_id)], [0, 2])

    def test_pending_includes_running_and_failed(self) -> None:
        job_id = self._job()
        self.db.update_unit(job_id, 0, status=STATUS_DONE, translated_text="x")
        self.db.update_unit(job_id, 1, status=STATUS_FAILED, error_msg="boom")
        self.db.update_unit(job_id, 2, status="running")
        self.assertEqual([u.unit_index for u in self.db.pending_units(job_id)], [1, 2])
        self.assertEqual(self.db.reset_failed(job_id), 1)
        self.assertEqual([u.unit_index for u in self.db.pending_units(job_id)], [1, 2])

    def test_delete_and_clear(self) -> None:
        first = self._job()
        second = self._job()
        self.db.delete_job(first)
        self.assertIsNone(self.db.job(first))
        self.assertEqual(len(self.db.units(first)), 0)
        self.assertEqual([j["job_id"] for j in self.db.jobs()], [second])
        self.db.clear()
        self.assertEqual(self.db.jobs(), [])

    def test_meta_json_roundtrip(self) -> None:
        unit = TranslationUnit(unit_index=0, source_text="x", meta={"mapping": {"⟦P0⟧": "`a`"}, "block": 2})
        job_id = self.db.create_job(title="t", parser_id="markdown", files=[])
        self.db.add_units(job_id, [unit])
        loaded = self.db.units(job_id)[0]
        self.assertEqual(loaded.meta["block"], 2)
        self.assertEqual(loaded.meta["mapping"], {"⟦P0⟧": "`a`"})


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.db = BatchJobDB(Path(self._tmp.name) / "jobs.db")
        self.addCleanup(self.db.close)
        self._original = runner_module.get_pipeline
        self.addCleanup(lambda: setattr(runner_module, "get_pipeline", self._original))

    def _install(self, fake: FakePipeline) -> FakePipeline:
        runner_module.get_pipeline = lambda: fake
        return fake

    def _job(self, count: int) -> str:
        job_id = self.db.create_job(title="j", parser_id="markdown", files=["a.md"], model="Index-Translate-9B")
        self.db.add_units(job_id, _units(count))
        return job_id

    def test_resume_skips_finished_units(self) -> None:
        job_id = self._job(20)
        for i in range(8):  # 模拟前 8 个已经翻完就崩了
            self.db.update_unit(job_id, i, status=STATUS_DONE, translated_text=f"done {i}")
        fake = self._install(FakePipeline())
        runner = BatchJobRunner(self.db)
        runner.enqueue(job_id)
        self.assertTrue(_pump(lambda: self.db.counts(job_id)["done"] == 20, 20.0))
        runner.wait(3000)
        self.assertEqual(len(fake.calls), 12)  # 只补翻 9..20
        self.assertEqual(fake.calls[0].text, "line 9")
        self.assertEqual(self.db.job(job_id)["status"], "done")

    def test_failed_unit_does_not_stop_queue(self) -> None:
        job_id = self._job(4)
        self._install(FakePipeline(fail_on={2}))
        runner = BatchJobRunner(self.db)
        runner.enqueue(job_id)
        self.assertTrue(_pump(lambda: self.db.counts(job_id)["done"] + self.db.counts(job_id)["failed"] == 4, 20.0))
        runner.wait(3000)
        units = self.db.units(job_id)
        self.assertEqual(units[1].status, STATUS_FAILED)
        self.assertIn("模拟失败", units[1].error_msg)
        self.assertEqual([u.status for u in units if u.unit_index != 1], [STATUS_DONE] * 3)

    def test_retry_failed_only_refetches_failure(self) -> None:
        job_id = self._job(3)
        self._install(FakePipeline(fail_on={1}))
        runner = BatchJobRunner(self.db)
        runner.enqueue(job_id)
        self.assertTrue(_pump(lambda: self.db.counts(job_id)["failed"] == 1, 20.0))
        runner.wait(3000)

        self.db.reset_failed(job_id)
        fake = self._install(FakePipeline())
        runner2 = BatchJobRunner(self.db)
        runner2.enqueue(job_id)
        self.assertTrue(_pump(lambda: self.db.counts(job_id)["done"] == 3, 20.0))
        runner2.wait(3000)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0].text, "line 1")

    def test_pause_blocks_new_units(self) -> None:
        job_id = self._job(3)
        fake = self._install(FakePipeline())
        runner = BatchJobRunner(self.db)
        runner.pause()
        runner.enqueue(job_id)
        self.assertTrue(_pump(lambda: runner.isRunning(), 5.0))
        time.sleep(0.4)
        self.assertEqual(fake.calls, [])  # 暂停时一个都不取
        runner.resume()
        self.assertTrue(_pump(lambda: len(fake.calls) == 3, 20.0))
        runner.wait(3000)

    def test_stop_returns_current_unit_to_pending(self) -> None:
        job_id = self._job(5)
        fake = self._install(FakePipeline(delay=0.6))
        runner = BatchJobRunner(self.db)
        runner.enqueue(job_id)
        self.assertTrue(_pump(lambda: len(fake.calls) >= 1, 10.0))
        runner.stop()
        statuses = [u.status for u in self.db.units(job_id)]
        self.assertIn(STATUS_PENDING, statuses)
        self.assertEqual(self.db.job(job_id)["status"], STATUS_PENDING)
        self.assertLess(len(fake.calls), 5)

    def test_skipped_units_are_counted_as_done(self) -> None:
        job_id = self.db.create_job(title="t", parser_id="markdown", files=["a.md"])
        self.db.add_units(
            job_id,
            [
                TranslationUnit(unit_index=0, source_text="```py\nprint(1)\n```", status="skipped"),
                TranslationUnit(unit_index=1, source_text="hello"),
            ],
        )
        self._install(FakePipeline())
        runner = BatchJobRunner(self.db)
        runner.enqueue(job_id)
        self.assertTrue(_pump(lambda: self.db.job(job_id)["status"] == "done", 15.0))
        runner.wait(3000)
        counts = self.db.counts(job_id)
        self.assertEqual(counts["done"], 2)
        self.assertEqual(counts["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
