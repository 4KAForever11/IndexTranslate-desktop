"""SQLite-backed response cache for the local proxy.

A translated web page is re-fetched constantly — re-render, tab switch, the same
article read twice. Caching the finished translation turns those into
millisecond hits and keeps the upstream free service out of the rate limiter.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key        TEXT PRIMARY KEY,
    model      TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used  REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_responses_used ON responses(last_used);
"""


class ProxyCache:
    def __init__(self, path: Path, *, max_entries: int = 2000, ttl_days: float = 7.0) -> None:
        self.path = Path(path)
        self.max_entries = max(0, int(max_entries))
        self.ttl_seconds = max(0.0, float(ttl_days) * 86400.0)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._conn = self._connect()
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.executescript(SCHEMA)
                self._conn.commit()
            except sqlite3.Error:
                pass

    def _connect(self) -> sqlite3.Connection:
        """Open the file, falling back to memory when the disk is unusable."""
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

    # ------------------------------------------------------------------- read
    def get(self, key: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT payload, last_used FROM responses WHERE key = ?", (key,)
                ).fetchone()
            except sqlite3.Error:
                self._misses += 1
                return None
            if row is None:
                self._misses += 1
                return None
            payload, last_used = str(row[0]), float(row[1])
            if self.ttl_seconds and now - last_used > self.ttl_seconds:
                try:
                    self._conn.execute("DELETE FROM responses WHERE key = ?", (key,))
                    self._conn.commit()
                except sqlite3.Error:
                    pass
                self._misses += 1
                return None
            try:
                self._conn.execute(
                    "UPDATE responses SET last_used = ?, hits = hits + 1 WHERE key = ?",
                    (now, key),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._hits += 1
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------ write
    def put(self, key: str, payload: dict[str, Any], model: str = "") -> None:
        if self.max_entries <= 0:
            return
        now = time.time()
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO responses (key, model, payload, created_at, last_used, hits)"
                    " VALUES (?,?,?,?,?,0)"
                    " ON CONFLICT(key) DO UPDATE SET payload = excluded.payload,"
                    " model = excluded.model, last_used = excluded.last_used",
                    (key, model, blob, now, now),
                )
                self._conn.commit()
            except sqlite3.Error:
                return
        self._prune()

    def _prune(self) -> None:
        """Drop expired rows, then keep only the most recently used entries."""
        now = time.time()
        with self._lock:
            try:
                if self.ttl_seconds:
                    self._conn.execute(
                        "DELETE FROM responses WHERE last_used < ?",
                        (now - self.ttl_seconds,),
                    )
                total = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
                if self.max_entries and total > self.max_entries:
                    self._conn.execute(
                        "DELETE FROM responses WHERE key IN ("
                        " SELECT key FROM responses ORDER BY last_used ASC LIMIT ?)",
                        (int(total) - self.max_entries,),
                    )
                self._conn.commit()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------ admin
    def clear(self) -> int:
        with self._lock:
            try:
                row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
                removed = int(row[0]) if row else 0
                self._conn.execute("DELETE FROM responses")
                self._conn.commit()
            except sqlite3.Error:
                return 0
        return removed

    def stats(self) -> dict[str, int]:
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM responses"
                ).fetchone()
                entries, hit_total = (int(row[0]), int(row[1])) if row else (0, 0)
            except sqlite3.Error:
                entries, hit_total = 0, 0
            return {
                "entries": entries,
                "lookups": self._hits + self._misses,
                "hits": self._hits,
                "hit_total": hit_total,
            }
