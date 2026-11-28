"""跨设备同步：快照生成与 LWW 合并。

原则（见 doc/DEVELOPMENT-V2-Final.md）：

* **不同步原始 SQLite 文件**——两边同时打开、或者拉到一半的快照，都会让 .db 损坏。
* 只同步「白名单」里的数据：术语表（分组 + 条目）、风格预设、少量通用偏好。
  不同步缓存、任务队列、扩展二进制、窗口位置与任何密钥。
* 每条记录带 ``uuid`` / ``updated_at`` / ``deleted``，合并时按 ``updated_at`` 取新（LWW），
  删除用墓碑（tombstone）传递，避免「A 删掉、B 又同步回来」。
* 合并前一定先把本地库复制成 ``.bak``。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SNAPSHOT_VERSION = 1
KIND_TERM = "glossary_term"
KIND_GROUP = "glossary_group"
KIND_STYLE = "style"
KIND_PREF = "pref"

#: 参与同步的偏好白名单（其余一律不同步，尤其是 api_base / 密钥 / 窗口几何）。
SYNC_PREF_KEYS: tuple[str, ...] = (
    "target_lang",
    "source_lang",
    "model",
    "stream",
    "use_glossary",
    "keep_history",
    "instruction",
    "active_style",
    "batch_export",
    "capture_target",
    "theme",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    uuid       TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0,
    deleted    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_meta_uuid ON meta (uuid);
"""


def _now() -> float:
    return round(time.time(), 3)


def _norm(text: str) -> str:
    return (text or "").strip().casefold()


@dataclass
class MergeStats:
    """一次合并的结果。"""

    added: int = 0
    updated: int = 0
    deleted: int = 0
    kept: int = 0
    prefs: int = 0
    styles: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "deleted": self.deleted,
            "kept": self.kept,
            "prefs": self.prefs,
            "styles": self.styles,
            "errors": list(self.errors),
        }

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"新增 {self.added}")
        if self.updated:
            parts.append(f"更新 {self.updated}")
        if self.deleted:
            parts.append(f"删除 {self.deleted}")
        if self.kept:
            parts.append(f"保留 {self.kept}")
        if self.prefs:
            parts.append(f"偏好 {self.prefs}")
        if self.styles:
            parts.append(f"风格 {self.styles}")
        text = "、".join(parts) or "没有变化"
        if self.errors:
            text += f"（{len(self.errors)} 条跳过）"
        return text


class SyncMetaDB:
    """同步元数据：为每条本地记录保存稳定的 uuid、修改时间与删除标记。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        except (sqlite3.Error, OSError):
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ 读
    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, key, uuid, updated_at, deleted FROM meta WHERE kind=? AND key=?",
                (kind, key),
            ).fetchone()
        return dict(row) if row else None

    def rows(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if kind is None:
                cursor = self._conn.execute(
                    "SELECT kind, key, uuid, updated_at, deleted FROM meta ORDER BY kind, key"
                )
            else:
                cursor = self._conn.execute(
                    "SELECT kind, key, uuid, updated_at, deleted FROM meta WHERE kind=? ORDER BY key",
                    (kind,),
                )
            return [dict(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------ 写
    def touch(self, kind: str, key: str, *, updated_at: float | None = None) -> dict[str, Any]:
        """记录被本地修改：刷新 updated_at，必要时生成 uuid。"""
        stamp = _now() if updated_at is None else float(updated_at)
        with self._lock:
            row = self._conn.execute(
                "SELECT uuid FROM meta WHERE kind=? AND key=?", (kind, key)
            ).fetchone()
            if row is None:
                record_id = uuidlib.uuid4().hex
                self._conn.execute(
                    "INSERT INTO meta (kind, key, uuid, updated_at, deleted) VALUES (?, ?, ?, ?, 0)",
                    (kind, key, record_id, stamp),
                )
            else:
                record_id = str(row["uuid"])
                self._conn.execute(
                    "UPDATE meta SET updated_at=?, deleted=0 WHERE kind=? AND key=?",
                    (stamp, kind, key),
                )
            self._conn.commit()
        return {"kind": kind, "key": key, "uuid": record_id, "updated_at": stamp, "deleted": 0}

    def mark_deleted(self, kind: str, key: str) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT uuid FROM meta WHERE kind=? AND key=?", (kind, key)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta (kind, key, uuid, updated_at, deleted) VALUES (?, ?, ?, ?, 1)",
                    (kind, key, uuidlib.uuid4().hex, _now()),
                )
            else:
                self._conn.execute(
                    "UPDATE meta SET deleted=1, updated_at=? WHERE kind=? AND key=?",
                    (_now(), kind, key),
                )
            self._conn.commit()

    def apply_remote(
        self,
        kind: str,
        key: str,
        *,
        record_id: str,
        updated_at: float,
        deleted: bool = False,
    ) -> None:
        """把远端的 uuid / 时间 / 删除标记写回来（合并之后调用）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (kind, key, uuid, updated_at, deleted) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, key) DO UPDATE SET uuid=excluded.uuid, "
                "updated_at=excluded.updated_at, deleted=excluded.deleted",
                (kind, key, record_id, float(updated_at), 1 if deleted else 0),
            )
            self._conn.commit()

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------- 快照
def _local_records(
    glossary_db,
    styles: Iterable[dict[str, Any]],
    settings: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    """把本地可同步数据摊平成 {(kind, key): payload}。"""
    records: dict[tuple[str, str], dict[str, Any]] = {}

    try:
        groups = glossary_db.groups()
    except Exception:  # 术语库异常不应该阻断同步
        groups = []
    for name, enabled in groups:
        key = _norm(name)
        if not key:
            continue
        records[(KIND_GROUP, key)] = {
            "name": name,
            "enabled": bool(enabled),
        }

    for group_name, _ in groups or [("", True)]:
        try:
            terms = glossary_db.all(group_name) if group_name else glossary_db.all()
        except Exception:
            terms = []
        for term, translation in terms:
            key = f"{_norm(group_name)}|{_norm(term)}"
            records[(KIND_TERM, key)] = {
                "term": term,
                "translation": translation,
                "group": group_name,
            }

    for style in styles:
        style_id = str(style.get("id") or "").strip()
        if not style_id:
            continue
        records[(KIND_STYLE, style_id)] = {
            "id": style_id,
            "name": str(style.get("name") or ""),
            "text": str(style.get("text") or ""),
        }

    for name in SYNC_PREF_KEYS:
        if name in settings:
            records[(KIND_PREF, name)] = {"key": name, "value": settings[name]}
    return records


def build_snapshot(
    glossary_db,
    *,
    styles: Iterable[dict[str, Any]] = (),
    settings: dict[str, Any] | None = None,
    meta: SyncMetaDB,
    device: str = "",
) -> dict[str, Any]:
    """生成本地快照（含墓碑）。

    本地已不存在的记录不会凭空消失：如果 meta 里还有它，就作为删除标记带出去，
    让对方也删掉，而不是在下一次拉取时又活过来。
    """
    settings = settings or {}
    current = _local_records(glossary_db, styles, settings)
    payload: list[dict[str, Any]] = []

    for (kind, key), data in sorted(current.items()):
        row = meta.get(kind, key)
        if row is None:
            row = meta.touch(kind, key)
        elif row["deleted"]:
            # 本地又加回来了：视为一次新写入
            row = meta.touch(kind, key)
        payload.append(
            {
                "kind": kind,
                "key": key,
                "uuid": row["uuid"],
                "updated_at": row["updated_at"],
                "deleted": False,
                "data": data,
            }
        )

    for row in meta.rows():
        if (row["kind"], row["key"]) in current:
            continue
        if row["deleted"]:
            payload.append(
                {
                    "kind": row["kind"],
                    "key": row["key"],
                    "uuid": row["uuid"],
                    "updated_at": row["updated_at"],
                    "deleted": True,
                    "data": {},
                }
            )
        else:
            meta.mark_deleted(row["kind"], row["key"])
            fresh = meta.get(row["kind"], row["key"]) or row
            payload.append(
                {
                    "kind": row["kind"],
                    "key": row["key"],
                    "uuid": fresh["uuid"],
                    "updated_at": fresh["updated_at"],
                    "deleted": True,
                    "data": {},
                }
            )

    return {
        "version": SNAPSHOT_VERSION,
        "app": "IndexTranslate desktop",
        "device": device,
        "exported_at": _now(),
        "records": payload,
    }


def snapshot_json(snapshot: dict[str, Any]) -> bytes:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")


def parse_snapshot(raw: bytes | str) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"同步文件无法解析：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError("同步文件格式不正确（缺少 records）。")
    version = int(data.get("version") or 0)
    if version > SNAPSHOT_VERSION:
        raise ValueError(f"同步文件来自更新的版本（v{version}），请先升级本程序。")
    return data


def backup_database(path: Path | str) -> Path | None:
    """合并前复制一份 .bak；文件不存在时返回 None。"""
    source = Path(path)
    if not source.exists():
        return None
    target = source.with_suffix(source.suffix + ".bak")
    try:
        shutil.copy2(source, target)
    except OSError:
        return None
    return target


def merge_snapshot(
    snapshot: dict[str, Any],
    glossary_db,
    *,
    meta: SyncMetaDB,
    settings_store=None,
) -> MergeStats:
    """把远端快照合并进本地（LWW）。``settings_store`` 需提供 ``set`` 与 ``save``。"""
    stats = MergeStats()
    settings_store = settings_store

    def remote_wins(kind: str, key: str, updated_at: float) -> bool:
        local = meta.get(kind, key)
        if local is None:
            return True
        return float(updated_at) > float(local["updated_at"])

    for entry in snapshot.get("records", []):
        try:
            kind = str(entry.get("kind") or "")
            key = str(entry.get("key") or "")
            record_id = str(entry.get("uuid") or "") or uuidlib.uuid4().hex
            updated_at = float(entry.get("updated_at") or 0.0)
            deleted = bool(entry.get("deleted"))
            data = entry.get("data") or {}
            if not kind or not key:
                stats.errors.append("记录缺少 kind/key")
                continue
            if not remote_wins(kind, key, updated_at):
                stats.kept += 1
                continue

            if kind == KIND_TERM:
                if deleted:
                    term = str(data.get("term") or key.split("|")[-1])
                    group = data.get("group") or None
                    glossary_db.delete(term, group)
                    stats.deleted += 1
                else:
                    term = str(data.get("term") or "").strip()
                    translation = str(data.get("translation") or "")
                    group = data.get("group") or None
                    if not term:
                        stats.errors.append("术语缺少原文")
                        continue
                    existed = glossary_db.get(term, group) is not None
                    glossary_db.upsert(term, translation, group)
                    stats.updated += 1 if existed else 0
                    stats.added += 0 if existed else 1
            elif kind == KIND_GROUP:
                name = str(data.get("name") or "").strip()
                if deleted:
                    if name:
                        glossary_db.delete_group(name)
                        stats.deleted += 1
                elif name:
                    glossary_db.ensure_group(name)
                    glossary_db.set_group_enabled(name, bool(data.get("enabled", True)))
                    stats.updated += 1
            elif kind == KIND_STYLE:
                if settings_store is None:
                    stats.kept += 1
                    continue
                styles = list(settings_store.get("styles", []) or [])
                style_id = str(data.get("id") or key)
                index = next(
                    (i for i, item in enumerate(styles) if str(item.get("id")) == style_id), None
                )
                if deleted:
                    if index is not None:
                        styles.pop(index)
                        settings_store.set("styles", styles)
                        settings_store.save()
                        stats.deleted += 1
                else:
                    record = {
                        "id": style_id,
                        "name": str(data.get("name") or style_id),
                        "text": str(data.get("text") or ""),
                    }
                    if index is None:
                        styles.append(record)
                        stats.styles += 1
                    else:
                        styles[index] = record
                        stats.updated += 1
                    settings_store.set("styles", styles)
                    settings_store.save()
            elif kind == KIND_PREF:
                if settings_store is None or deleted:
                    stats.kept += 1
                    continue
                name = str(data.get("key") or key)
                if name in SYNC_PREF_KEYS:
                    settings_store.set(name, data.get("value"))
                    settings_store.save()
                    stats.prefs += 1
                else:
                    stats.kept += 1
                    continue
            else:
                stats.errors.append(f"未知记录类型：{kind}")
                continue

            meta.apply_remote(kind, key, record_id=record_id, updated_at=updated_at, deleted=deleted)
        except Exception as exc:  # 单条失败不影响整次合并
            stats.errors.append(f"{entry.get('key', '')}: {exc}")
    return stats