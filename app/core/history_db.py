"""SQLite-backed translation history.

Every write goes through one connection guarded by a lock, which is plenty for
a single-user desktop app and avoids the "database is locked" surprises that
thread-local connections cause under rapid streaming updates.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'text',
    source_lang TEXT    NOT NULL DEFAULT '',
    target_lang TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    source_text TEXT    NOT NULL DEFAULT '',
    result_text TEXT    NOT NULL DEFAULT '',
    image_thumb BLOB,
    elapsed_ms  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts DESC);
"""


@dataclass
class HistoryEntry:
    id: int
    ts: str
    kind: str
    source_lang: str
    target_lang: str
    model: str
    source_text: str
    result_text: str
    image_thumb: Optional[bytes]
    elapsed_ms: int

    @property
    def when(self) -> str:
        try:
            moment = datetime.fromisoformat(self.ts)
        except ValueError:
            return self.ts
        today = datetime.now().date()
        if moment.date() == today:
            return moment.strftime("今天 %H:%M")
        if moment.year == today.year:
            return moment.strftime("%m-%d %H:%M")
        return moment.strftime("%Y-%m-%d %H:%M")

    def preview(self) -> str:
        text = (self.source_text or "").strip() or "图片翻译"
        return text.replace("\n", " ")[:80]


class HistoryDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = self._connect()
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _connect(self) -> sqlite3.Connection:
        """Open the database, falling back to memory when the disk is unusable.

        A locked-down profile can make the folder (and therefore the WAL
        side-files) unwritable. Losing the history is acceptable; refusing to
        start is not.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return sqlite3.connect(str(self.path), check_same_thread=False)
        except (sqlite3.Error, OSError):
            return sqlite3.connect(":memory:", check_same_thread=False)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ write
    def add(
        self,
        *,
        kind: str,
        source_text: str,
        result_text: str,
        source_lang: str = "",
        target_lang: str = "",
        model: str = "",
        image_thumb: Optional[bytes] = None,
        elapsed_ms: int = 0,
        keep: int = 0,
    ) -> int:
        ts = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO history (ts, kind, source_lang, target_lang, model,"
                " source_text, result_text, image_thumb, elapsed_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ts,
                    kind,
                    source_lang,
                    target_lang,
                    model,
                    source_text,
                    result_text,
                    sqlite3.Binary(image_thumb) if image_thumb else None,
                    int(elapsed_ms),
                ),
            )
            self._conn.commit()
            entry_id = int(cursor.lastrowid or 0)
            if keep and keep > 0:
                self._trim_locked(keep)
                self._conn.commit()
            return entry_id

    def _trim_locked(self, keep: int) -> None:
        self._conn.execute(
            "DELETE FROM history WHERE id NOT IN"
            " (SELECT id FROM history ORDER BY id DESC LIMIT ?)",
            (keep,),
        )

    def trim(self, keep: int) -> None:
        if keep <= 0:
            return
        with self._lock:
            self._trim_locked(keep)
            self._conn.commit()

    def delete(self, entry_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM history WHERE id = ?", (entry_id,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM history")
            self._conn.commit()

    # ------------------------------------------------------------------- read
    def search(self, keyword: str = "", limit: int = 100, offset: int = 0) -> list[HistoryEntry]:
        keyword = (keyword or "").strip()
        sql = "SELECT * FROM history"
        params: list[object] = []
        if keyword:
            like = f"%{keyword}%"
            sql += " WHERE source_text LIKE ? OR result_text LIKE ?"
            params.extend([like, like])
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def count(self, keyword: str = "") -> int:
        keyword = (keyword or "").strip()
        sql = "SELECT COUNT(*) FROM history"
        params: list[object] = []
        if keyword:
            like = f"%{keyword}%"
            sql += " WHERE source_text LIKE ? OR result_text LIKE ?"
            params.extend([like, like])
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def get(self, entry_id: int) -> Optional[HistoryEntry]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM history WHERE id = ?", (entry_id,)).fetchone()
        return self._row_to_entry(row) if row else None

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> HistoryEntry:
        thumb = row["image_thumb"]
        return HistoryEntry(
            id=int(row["id"]),
            ts=str(row["ts"]),
            kind=str(row["kind"]),
            source_lang=str(row["source_lang"]),
            target_lang=str(row["target_lang"]),
            model=str(row["model"]),
            source_text=str(row["source_text"]),
            result_text=str(row["result_text"]),
            image_thumb=bytes(thumb) if thumb else None,
            elapsed_ms=int(row["elapsed_ms"]),
        )
