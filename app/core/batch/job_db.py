"""批量任务持久化：任务与单元的 SQLite 存档。

每个单元翻译完成就立即落库，所以进程被杀掉后重启还能从断点继续（断点续翻）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from ..parsers.base import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    TranslationUnit,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_jobs (
    job_id        TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    parser_id     TEXT NOT NULL DEFAULT '',
    files         TEXT NOT NULL DEFAULT '[]',
    source_lang   TEXT NOT NULL DEFAULT 'auto',
    target_lang   TEXT NOT NULL DEFAULT 'zh',
    model         TEXT NOT NULL DEFAULT '',
    glossary_group TEXT,
    export_mode   TEXT NOT NULL DEFAULT 'bilingual',
    status        TEXT NOT NULL DEFAULT 'pending',
    total         INTEGER NOT NULL DEFAULT 0,
    done          INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS batch_units (
    job_id          TEXT NOT NULL,
    unit_index      INTEGER NOT NULL,
    source_text     TEXT NOT NULL DEFAULT '',
    image_path      TEXT,
    context_hint    TEXT NOT NULL DEFAULT '',
    meta_json       TEXT NOT NULL DEFAULT '{}',
    translated_text TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    error_msg       TEXT NOT NULL DEFAULT '',
    warn            TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, unit_index)
);

CREATE INDEX IF NOT EXISTS idx_units_status ON batch_units (job_id, status);
"""

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_FAILED)


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


class BatchJobDB:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = self._connect()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ infra
    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            return conn
        except (sqlite3.Error, OSError):
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------- jobs
    def create_job(
        self,
        *,
        title: str,
        parser_id: str,
        files: Iterable[Path | str],
        source_lang: str = "auto",
        target_lang: str = "zh",
        model: str = "",
        glossary_group: Optional[str] = None,
        export_mode: str = "bilingual",
        status: str = STATUS_PENDING,
    ) -> str:
        job_id = new_job_id()
        now = time.time()
        payload = [str(Path(f)) for f in files]
        with self._lock:
            self._conn.execute(
                "INSERT INTO batch_jobs (job_id, title, parser_id, files, source_lang,"
                " target_lang, model, glossary_group, export_mode, status, total, done,"
                " failed, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,0,0,0,?,?)",
                (
                    job_id,
                    title,
                    parser_id,
                    json.dumps(payload, ensure_ascii=False),
                    source_lang,
                    target_lang,
                    model,
                    glossary_group,
                    export_mode,
                    status,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return job_id

    def job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job_row(row) if row else None

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batch_jobs ORDER BY created_at DESC"
            ).fetchall()
        return [_job_row(r) for r in rows]

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.jobs()[: max(1, limit)]

    def set_status(self, job_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE batch_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (status, time.time(), job_id),
            )
            self._conn.commit()

    def set_export_mode(self, job_id: str, mode: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE batch_jobs SET export_mode = ?, updated_at = ? WHERE job_id = ?",
                (mode, time.time(), job_id),
            )
            self._conn.commit()

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM batch_units WHERE job_id = ?", (job_id,))
            self._conn.execute("DELETE FROM batch_jobs WHERE job_id = ?", (job_id,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM batch_units")
            self._conn.execute("DELETE FROM batch_jobs")
            self._conn.commit()

    # ------------------------------------------------------------------ units
    def add_units(self, job_id: str, units: Iterable[TranslationUnit]) -> int:
        rows = []
        for unit in units:
            rows.append(
                (
                    job_id,
                    int(unit.unit_index),
                    unit.source_text or "",
                    unit.image_path,
                    unit.context_hint or "",
                    json.dumps(unit.meta or {}, ensure_ascii=False),
                    unit.translated_text or "",
                    unit.status or STATUS_PENDING,
                    unit.error_msg or "",
                    unit.warn or "",
                )
            )
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO batch_units (job_id, unit_index, source_text,"
                " image_path, context_hint, meta_json, translated_text, status, error_msg,"
                " warn) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.execute(
                "UPDATE batch_jobs SET total = ?, updated_at = ? WHERE job_id = ?",
                (len(rows), time.time(), job_id),
            )
            self._conn.commit()
        return len(rows)

    def update_unit(
        self,
        job_id: str,
        unit_index: int,
        *,
        status: Optional[str] = None,
        translated_text: Optional[str] = None,
        error_msg: Optional[str] = None,
        warn: Optional[str] = None,
    ) -> None:
        fields: list[str] = []
        values: list[Any] = []
        for name, value in (
            ("status", status),
            ("translated_text", translated_text),
            ("error_msg", error_msg),
            ("warn", warn),
        ):
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        if not fields:
            return
        values += [job_id, int(unit_index)]
        with self._lock:
            self._conn.execute(
                f"UPDATE batch_units SET {', '.join(fields)} WHERE job_id = ? AND unit_index = ?",
                values,
            )
            self._conn.commit()

    def units(self, job_id: str) -> list[TranslationUnit]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batch_units WHERE job_id = ? ORDER BY unit_index",
                (job_id,),
            ).fetchall()
        return [_unit_row(r) for r in rows]

    def unit(self, job_id: str, unit_index: int) -> Optional[TranslationUnit]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batch_units WHERE job_id = ? AND unit_index = ?",
                (job_id, int(unit_index)),
            ).fetchone()
        return _unit_row(row) if row else None

    def pending_units(self, job_id: str) -> list[TranslationUnit]:
        """待办单元：未完成 + 上次被打断的（running）+ 失败的。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batch_units WHERE job_id = ? AND status IN (?,?,?)"
                " ORDER BY unit_index",
                (job_id, STATUS_PENDING, STATUS_RUNNING, STATUS_FAILED),
            ).fetchall()
        return [_unit_row(r) for r in rows]

    def failed_units(self, job_id: str) -> list[TranslationUnit]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batch_units WHERE job_id = ? AND status = ? ORDER BY unit_index",
                (job_id, STATUS_FAILED),
            ).fetchall()
        return [_unit_row(r) for r in rows]

    def reset_failed(self, job_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE batch_units SET status = ?, error_msg = '' WHERE job_id = ? AND status = ?",
                (STATUS_PENDING, job_id, STATUS_FAILED),
            )
            self._conn.commit()
        return cur.rowcount or 0

    def counts(self, job_id: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM batch_units WHERE job_id = ? GROUP BY status",
                (job_id,),
            ).fetchall()
        result = {status: 0 for status in (STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED)}
        for row in rows:
            result[str(row["status"])] = int(row["n"])
        total = sum(result.values())
        done = result[STATUS_DONE] + result[STATUS_SKIPPED]
        failed = result[STATUS_FAILED]
        with self._lock:
            self._conn.execute(
                "UPDATE batch_jobs SET total = ?, done = ?, failed = ?, updated_at = ?"
                " WHERE job_id = ?",
                (total, done, failed, time.time(), job_id),
            )
            self._conn.commit()
        return {
            "total": total,
            "done": done,  # 已完成 + 原样保留
            "failed": failed,
            "pending": result[STATUS_PENDING],
            "running": result[STATUS_RUNNING],
            "skipped": result[STATUS_SKIPPED],
        }

    def unfinished_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batch_jobs WHERE status IN (?,?) ORDER BY created_at",
                (STATUS_PENDING, STATUS_RUNNING),
            ).fetchall()
        return [_job_row(r) for r in rows]


# --------------------------------------------------------------------- mappers
def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["files"] = json.loads(data.get("files") or "[]")
    except json.JSONDecodeError:
        data["files"] = []
    return data


def _unit_row(row: sqlite3.Row) -> TranslationUnit:
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return TranslationUnit(
        unit_index=int(row["unit_index"]),
        source_text=row["source_text"] or "",
        image_path=row["image_path"],
        context_hint=row["context_hint"] or "",
        meta=meta,
        translated_text=row["translated_text"] or "",
        status=row["status"] or STATUS_PENDING,
        error_msg=row["error_msg"] or "",
        warn=row["warn"] or "",
    )
