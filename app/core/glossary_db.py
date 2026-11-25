"""SQLite-backed glossary (term → translation) used to steer the model."""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS terms (
    term        TEXT PRIMARY KEY COLLATE NOCASE,
    translation TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT ''
);
"""

# Accepts the shapes people actually paste: "A=B", "A → B", "A\tB", "A:B".
_SPLIT = re.compile(r"\s*(?:=>|→|->|=|:|\t)\s*")


class GlossaryDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = self._connect()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _connect(self) -> sqlite3.Connection:
        """Open the database, falling back to memory when the disk is unusable.

        A locked-down profile can make the folder (and therefore the WAL
        side-files) unwritable. Losing the glossary is acceptable; refusing to
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
    def upsert(self, term: str, translation: str = "") -> None:
        term = (term or "").strip()
        if not term:
            return
        from datetime import datetime

        with self._lock:
            self._conn.execute(
                "INSERT INTO terms (term, translation, created_at) VALUES (?,?,?)"
                " ON CONFLICT(term) DO UPDATE SET translation = excluded.translation",
                (term, (translation or "").strip(), datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def delete(self, term: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM terms WHERE term = ?", (term,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM terms")
            self._conn.commit()

    def import_lines(self, lines: Iterable[str]) -> int:
        """Bulk import; returns how many rows were written."""
        added = 0
        with self._lock:
            for line in lines:
                line = (line or "").strip().lstrip("\ufeff")
                if not line or line.startswith("#"):
                    continue
                parts = _SPLIT.split(line, maxsplit=1)
                term = parts[0].strip()
                translation = parts[1].strip() if len(parts) > 1 else ""
                if not term:
                    continue
                self._conn.execute(
                    "INSERT INTO terms (term, translation) VALUES (?,?)"
                    " ON CONFLICT(term) DO UPDATE SET translation = excluded.translation",
                    (term, translation),
                )
                added += 1
            self._conn.commit()
        return added

    # ------------------------------------------------------------------- read
    def all(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT term, translation FROM terms ORDER BY term COLLATE NOCASE"
            ).fetchall()
        return [(str(term), str(translation or "")) for term, translation in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM terms").fetchone()
        return int(row[0]) if row else 0

    def get(self, term: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT translation FROM terms WHERE term = ?", ((term or "").strip(),)
            ).fetchone()
        return None if row is None else str(row[0] or "")

    def export_text(self) -> str:
        return "\n".join(f"{term}\t{translation}" for term, translation in self.all())

    def prompt_string(self) -> str:
        """The '、'-joined A→B list consumed by prompt_builder."""
        pairs = [f"{term}→{translation}" for term, translation in self.all() if translation]
        return "、".join(pairs)
