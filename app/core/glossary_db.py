"""SQLite-backed glossary (term → translation) with per-domain groups.

Terms live in *groups* (场景/领域): 游戏、开发、医学…… 只有启用的分组才会参与
提示词注入，这样同一个词在不同领域可以有不同的译法而不会互相冲突；注入时还会
按原文实际命中的词条裁剪，避免上百条术语把 Prompt 撑大、拖慢首字延迟。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_GROUP = "默认"
SCHEMA_VERSION = 2

SCHEMA_GROUPS = """
CREATE TABLE IF NOT EXISTS groups (
    name    TEXT PRIMARY KEY COLLATE NOCASE,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort    INTEGER NOT NULL DEFAULT 0
);
"""

SCHEMA_TERMS = """
CREATE TABLE IF NOT EXISTS terms (
    term        TEXT NOT NULL COLLATE NOCASE,
    translation TEXT NOT NULL DEFAULT '',
    group_name  TEXT NOT NULL DEFAULT '默认',
    created_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (term, group_name)
);
"""

SCHEMA = SCHEMA_GROUPS + SCHEMA_TERMS + """
CREATE INDEX IF NOT EXISTS idx_terms_group ON terms (group_name);
"""

# Accepts the shapes people actually paste: "A=B", "A → B", "A\tB", "A:B".
_SPLIT = re.compile(r"\s*(?:=>|→|->|=|:|\t)\s*")

# 注入上限：超过这个数量只保留最长的那些，避免 Prompt 失控
MAX_INJECT_TERMS = 40


class GlossaryDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = self._connect()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._migrate()
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO groups (name, enabled, sort) VALUES (?, 1, 0)",
                (DEFAULT_GROUP,),
            )
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

    # -------------------------------------------------------------- migration
    def _migrate(self) -> None:
        """v1 (term PK, no groups) → v2 (term + group_name composite PK)."""
        try:
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        except (sqlite3.Error, TypeError):
            version = 0
        if version >= SCHEMA_VERSION:
            return
        exists = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='terms'"
        ).fetchone()
        if exists:
            columns = [row[1] for row in self._conn.execute("PRAGMA table_info(terms)")]
            if "group_name" in columns:
                rows = self._conn.execute(
                    "SELECT term, translation, group_name, created_at FROM terms"
                ).fetchall()
            else:
                rows = [
                    (term, translation, DEFAULT_GROUP, created_at)
                    for term, translation, created_at in self._conn.execute(
                        "SELECT term, translation, created_at FROM terms"
                    )
                ]
            self._conn.execute("DROP TABLE terms")
            self._conn.execute(SCHEMA_TERMS)
            self._conn.executemany(
                "INSERT OR REPLACE INTO terms (term, translation, group_name, created_at)"
                " VALUES (?,?,?,?)",
                rows,
            )
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ----------------------------------------------------------------- groups
    def groups(self) -> list[tuple[str, bool]]:
        """``[(name, enabled), ...]`` ordered for the UI."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, enabled FROM groups ORDER BY sort, name COLLATE NOCASE"
            ).fetchall()
        return [(str(name), bool(enabled)) for name, enabled in rows]

    def group_names(self) -> list[str]:
        return [name for name, _ in self.groups()]

    def enabled_groups(self) -> list[str]:
        return [name for name, enabled in self.groups() if enabled]

    def ensure_group(self, name: str) -> str:
        name = (name or "").strip() or DEFAULT_GROUP
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO groups (name, enabled, sort) VALUES (?, 1,"
                " (SELECT COALESCE(MAX(sort), 0) + 1 FROM groups))",
                (name,),
            )
            self._conn.commit()
        return name

    def set_group_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE groups SET enabled = ? WHERE name = ?", (1 if enabled else 0, name)
            )
            self._conn.commit()

    def rename_group(self, old: str, new: str) -> None:
        new = (new or "").strip() or DEFAULT_GROUP
        if old == new:
            return
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO groups (name) VALUES (?)", (new,))
            self._conn.execute(
                "UPDATE OR REPLACE terms SET group_name = ? WHERE group_name = ?", (new, old)
            )
            self._conn.execute("DELETE FROM groups WHERE name = ?", (old,))
            self._conn.commit()

    def delete_group(self, name: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM terms WHERE group_name = ?", (name,))
            self._conn.execute("DELETE FROM groups WHERE name = ?", (name,))
            row = self._conn.execute("SELECT COUNT(*) FROM groups").fetchone()
            if not row or not int(row[0]):
                self._conn.execute(
                    "INSERT OR IGNORE INTO groups (name, enabled, sort) VALUES (?, 1, 0)",
                    (DEFAULT_GROUP,),
                )
            self._conn.commit()

    # ------------------------------------------------------------------ write
    def upsert(self, term: str, translation: str = "", group: Optional[str] = None) -> None:
        term = (term or "").strip()
        if not term:
            return
        group_name = (group or "").strip() or DEFAULT_GROUP
        self.ensure_group(group_name)
        with self._lock:
            self._conn.execute(
                "INSERT INTO terms (term, translation, group_name, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT(term, group_name) DO UPDATE SET translation = excluded.translation",
                (term, (translation or "").strip(), group_name, datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def delete(self, term: str, group: Optional[str] = None) -> None:
        term = (term or "").strip()
        with self._lock:
            if group is None:
                self._conn.execute("DELETE FROM terms WHERE term = ?", (term,))
            else:
                self._conn.execute(
                    "DELETE FROM terms WHERE term = ? AND group_name = ?", (term, group)
                )
            self._conn.commit()

    def clear(self, group: Optional[str] = None) -> None:
        with self._lock:
            if group is None:
                self._conn.execute("DELETE FROM terms")
            else:
                self._conn.execute("DELETE FROM terms WHERE group_name = ?", (group,))
            self._conn.commit()

    def import_lines(self, lines: Iterable[str], group: Optional[str] = None) -> int:
        """Bulk import; returns how many rows were written."""
        group_name = (group or "").strip() or DEFAULT_GROUP
        self.ensure_group(group_name)
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
                    "INSERT INTO terms (term, translation, group_name) VALUES (?,?,?)"
                    " ON CONFLICT(term, group_name) DO UPDATE SET translation = excluded.translation",
                    (term, translation, group_name),
                )
                added += 1
            self._conn.commit()
        return added

    # ------------------------------------------------------------------- read
    def all(self, group: Optional[str] = None) -> list[tuple[str, str]]:
        """``[(term, translation), ...]`` — one group, or everything."""
        with self._lock:
            if group is None:
                rows = self._conn.execute(
                    "SELECT term, translation FROM terms ORDER BY term COLLATE NOCASE"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT term, translation FROM terms WHERE group_name = ?"
                    " ORDER BY term COLLATE NOCASE",
                    (group,),
                ).fetchall()
        return [(str(term), str(translation or "")) for term, translation in rows]

    def all_enabled(self) -> list[tuple[str, str]]:
        """Every term whose group is switched on."""
        names = self.enabled_groups()
        if not names or len(names) == len(self.group_names()):
            return self.all()
        placeholders = ",".join("?" * len(names))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT term, translation FROM terms WHERE group_name IN ({placeholders})"
                " ORDER BY term COLLATE NOCASE",
                tuple(names),
            ).fetchall()
        return [(str(term), str(translation or "")) for term, translation in rows]

    def count(self, group: Optional[str] = None) -> int:
        with self._lock:
            if group is None:
                row = self._conn.execute("SELECT COUNT(*) FROM terms").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM terms WHERE group_name = ?", (group,)
                ).fetchone()
        return int(row[0]) if row else 0

    def get(self, term: str, group: Optional[str] = None) -> Optional[str]:
        with self._lock:
            if group is None:
                row = self._conn.execute(
                    "SELECT translation FROM terms WHERE term = ?", ((term or "").strip(),)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT translation FROM terms WHERE term = ? AND group_name = ?",
                    ((term or "").strip(), group),
                ).fetchone()
        return None if row is None else str(row[0] or "")

    def export_text(self, group: Optional[str] = None) -> str:
        return "\n".join(
            f"{term}\t{translation}" for term, translation in self.all(group)
        )

    # -------------------------------------------------------------- injection
    def match_pairs(self, text: str, *, max_terms: int = MAX_INJECT_TERMS) -> list[tuple[str, str]]:
        """只保留原文里真的出现过的术语，长词优先（API 网关 胜过 API）。"""
        folded = (text or "").casefold()
        if not folded:
            return []
        hits = [
            (term, translation)
            for term, translation in self.all_enabled()
            if translation and term.casefold() in folded
        ]
        hits.sort(key=lambda pair: (-len(pair[0]), pair[0]))
        return hits[:max_terms]

    def prompt_for(self, text: str, *, max_terms: int = MAX_INJECT_TERMS) -> str:
        """按命中裁剪后的 A→B 列表；没有命中就是空串（不注入）。"""
        return "、".join(f"{term}→{translation}" for term, translation in self.match_pairs(text, max_terms=max_terms))

    def prompt_string(self, group: Optional[str] = None) -> str:
        """All enabled pairs (or one group) — used when there is no source text."""
        pairs = self.all(group) if group is not None else self.all_enabled()
        return "、".join(f"{term}→{translation}" for term, translation in pairs if translation)
