"""Translation result cache (app side).

Distinct from :mod:`app.core.proxy_cache`, which stores *upstream response
bodies* for arbitrary passthrough clients. This one stores finished translation
**text** keyed by everything that can change the answer — source text, language
pair, model, instruction and the glossary terms that were injected.

The glossary terms are part of the key on purpose: editing a term list must
invalidate the affected entries instead of serving yesterday's wording.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS translation_cache (
    cache_key   TEXT PRIMARY KEY,
    result_text TEXT NOT NULL,
    engine_used TEXT NOT NULL DEFAULT '',
    model_used  TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_translation_cache_used ON translation_cache(updated_at);
"""


def make_key(
    text: str,
    source_lang: str,
    target_lang: str,
    model: str,
    instruction: str = "",
    matched_terms: list[str] | None = None,
) -> str:
    """Stable key over every input that changes the translation."""
    blob = json.dumps(
        [
            text,
            source_lang,
            target_lang,
            model,
            instruction or "",
            list(matched_terms or []),
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TranslationCacheDB:
    def __init__(
        self,
        db_path: Path | str,
        ttl_sec: float = 7 * 86400,
        max_rows: int = 10000,
    ) -> None:
        self.path = Path(db_path)
        self.ttl_sec = max(0.0, float(ttl_sec))
        self.max_rows = max(0, int(max_rows))
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._conn = self._open_db()
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.executescript(SCHEMA)
                self._conn.commit()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------ plumbing
    def _open_db(self) -> sqlite3.Connection:
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

    # ----------------------------------------------------------------------- read
    def get(self, cache_key: str) -> tuple[str, str, str] | None:
        """``(result_text, engine_used, model_used)`` or ``None``."""
        if not cache_key:
            return None
        now = time.time()
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT result_text, engine_used, model_used, updated_at"
                    " FROM translation_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
                if row is None:
                    self._misses += 1
                    return None
                text, engine, model, updated_at = str(row[0]), str(row[1]), str(row[2]), float(row[3])
                if self.ttl_sec and now - updated_at > self.ttl_sec:
                    self._conn.execute(
                        "DELETE FROM translation_cache WHERE cache_key = ?", (cache_key,)
                    )
                    self._conn.commit()
                    self._misses += 1
                    return None
                self._hits += 1
                return text, engine, model
            except sqlite3.Error:
                self._misses += 1
                return None

    # ---------------------------------------------------------------------- write
    def set(self, cache_key: str, result_text: str, engine_used: str = "", model_used: str = "") -> None:
        if not cache_key or not (result_text or "").strip():
            return
        if self.max_rows <= 0:  # cache disabled
            return
        now = time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO translation_cache"
                    " (cache_key, result_text, engine_used, model_used, updated_at)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(cache_key) DO UPDATE SET"
                    "   result_text = excluded.result_text,"
                    "   engine_used = excluded.engine_used,"
                    "   model_used  = excluded.model_used,"
                    "   updated_at  = excluded.updated_at",
                    (cache_key, result_text, engine_used or "", model_used or "", now),
                )
                self._conn.commit()
            except sqlite3.Error:
                return
        self._prune()

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            try:
                if self.ttl_sec:
                    self._conn.execute(
                        "DELETE FROM translation_cache WHERE updated_at < ?",
                        (now - self.ttl_sec,),
                    )
                if self.max_rows:
                    row = self._conn.execute("SELECT COUNT(*) FROM translation_cache").fetchone()
                    total = int(row[0]) if row else 0
                    if total > self.max_rows:
                        self._conn.execute(
                            "DELETE FROM translation_cache WHERE cache_key IN ("
                            " SELECT cache_key FROM translation_cache"
                            " ORDER BY updated_at ASC LIMIT ?)",
                            (total - self.max_rows,),
                        )
                self._conn.commit()
            except sqlite3.Error:
                pass

    # ---------------------------------------------------------------------- admin
    def clear(self) -> int:
        with self._lock:
            try:
                row = self._conn.execute("SELECT COUNT(*) FROM translation_cache").fetchone()
                removed = int(row[0]) if row else 0
                self._conn.execute("DELETE FROM translation_cache")
                self._conn.commit()
            except sqlite3.Error:
                return 0
        return removed

    def stats(self) -> dict[str, int]:
        with self._lock:
            try:
                row = self._conn.execute("SELECT COUNT(*) FROM translation_cache").fetchone()
                entries = int(row[0]) if row else 0
            except sqlite3.Error:
                entries = 0
            lookups = self._hits + self._misses
            return {"entries": entries, "lookups": lookups, "hits": self._hits}
