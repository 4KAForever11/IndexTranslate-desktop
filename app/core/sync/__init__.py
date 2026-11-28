"""跨设备同步与自动更新。

* :mod:`snapshot` —— 白名单快照 + LWW 合并（不搬 SQLite 文件）。
* :mod:`webdav_sync` —— WebDAV 上传 / 下载 / 探活，密码走 DPAPI。
"""
from __future__ import annotations

from .snapshot import (
    KIND_GROUP,
    KIND_PREF,
    KIND_STYLE,
    KIND_TERM,
    SNAPSHOT_VERSION,
    MergeStats,
    SyncMetaDB,
    backup_database,
    build_snapshot,
    merge_snapshot,
    parse_snapshot,
    snapshot_json,
)
from .webdav_sync import SyncResult, SyncService, WebDAVClient, WebDAVError

__all__ = [
    "KIND_GROUP",
    "KIND_PREF",
    "KIND_STYLE",
    "KIND_TERM",
    "SNAPSHOT_VERSION",
    "MergeStats",
    "SyncMetaDB",
    "SyncResult",
    "SyncService",
    "WebDAVClient",
    "WebDAVError",
    "backup_database",
    "build_snapshot",
    "merge_snapshot",
    "parse_snapshot",
    "snapshot_json",
]