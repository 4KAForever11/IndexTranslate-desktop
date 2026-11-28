"""扩展运行时：谁在跑、跑多久、什么时候该回收。

UI 只跟这个对象打交道：它按需拉起 Sidecar、按空闲时间回收、提供状态快照。
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from ..config import VERSION, data_dir, settings
from .manager import ExtensionManager, InstalledExtension
from .sidecar_client import SidecarClient, SidecarError

DEFAULT_IDLE_SEC = 300.0


class ExtensionRuntime(QObject):
    """已安装扩展的进程管理（懒启动 + 空闲回收）。"""

    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.manager = ExtensionManager(data_dir() / "extensions")
        self.manager.cleanup_leftovers()
        # 安装/升级前先停掉同名子进程，否则 Windows 不让替换正在运行的 exe
        self.manager.stop_hook = self.stop
        self._clients: dict[str, SidecarClient] = {}
        # 模型目录落盘在 settings 里，重启后要读回来，否则字幕永远认为「没选模型」
        self._models: dict[str, str] = {
            "ext-live-asr": str(settings.get("live_asr_model", "") or "")
        }

    # ---------------------------------------------------------------- 基础
    @property
    def host_version(self) -> str:
        return VERSION

    def idle_timeout(self) -> float:
        try:
            return float(settings.get("extension_idle_sec", DEFAULT_IDLE_SEC) or DEFAULT_IDLE_SEC)
        except (TypeError, ValueError):
            return DEFAULT_IDLE_SEC

    def installed(self) -> list[InstalledExtension]:
        return self.manager.installed()

    def get(self, ext_id: str) -> InstalledExtension | None:
        return self.manager.get(ext_id)

    def is_enabled(self, ext_id: str) -> bool:
        item = self.manager.get(ext_id)
        return bool(item and item.enabled)

    def set_enabled(self, ext_id: str, enabled: bool) -> None:
        self.manager.set_enabled(ext_id, enabled)
        if not enabled:
            self.stop(ext_id)
        self.changed.emit()

    def uninstall(self, ext_id: str) -> bool:
        self.stop(ext_id)
        removed = self.manager.uninstall(ext_id)
        self.changed.emit()
        return removed

    # ---------------------------------------------------------------- 进程
    def client(self, ext_id: str) -> SidecarClient | None:
        item = self.manager.get(ext_id)
        if item is None or not item.enabled:
            return None
        client = self._clients.get(ext_id)
        if client is None or client.entry != item.entry_path:
            if client is not None:
                client.stop()
            client = item.client(idle_timeout_sec=self.idle_timeout())
            self._clients[ext_id] = client
        return client

    def running_client(self, ext_id: str) -> SidecarClient | None:
        client = self._clients.get(ext_id)
        return client if client and client.running else None

    def stop(self, ext_id: str) -> None:
        client = self._clients.pop(ext_id, None)
        if client is not None:
            client.stop()

    def stop_all(self) -> None:
        for client in list(self._clients.values()):
            try:
                client.stop()
            except Exception:
                continue
        self._clients.clear()

    def reclaim_idle(self) -> int:
        """回收空闲进程，返回回收个数（由主窗口的定时器周期调用）。"""
        reclaimed = 0
        for ext_id, client in list(self._clients.items()):
            try:
                if client.reclaim_if_idle():
                    reclaimed += 1
            except Exception:
                continue
            if not client.running:
                self._clients.pop(ext_id, None)
        if reclaimed:
            self.changed.emit()
        return reclaimed

    # ---------------------------------------------------------------- 状态
    def status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.installed():
            client = self._clients.get(item.manifest.id)
            rows.append(
                {
                    "extension": item,
                    "id": item.manifest.id,
                    "name": item.manifest.name,
                    "version": item.manifest.version,
                    "enabled": item.enabled,
                    "host_ok": item.manifest.requires_host(self.host_version),
                    "host_error": item.manifest.host_error(self.host_version),
                    "running": bool(client and client.running),
                    "pid": client.pid if client else None,
                    "restarts": client.restarts if client else 0,
                    "size": item.size_bytes,
                    "description": item.manifest.description,
                }
            )
        return rows

    def ping(self, ext_id: str) -> tuple[bool, str]:
        client = self.client(ext_id)
        if client is None:
            return False, "扩展未安装或已停用。"
        try:
            result = client.ping(timeout=25.0)
        except SidecarError as exc:
            return False, str(exc)
        deps = result.get("deps")
        note = f"版本 {result.get('version', '?')}"
        if isinstance(deps, dict):
            missing = [name for name, ok in deps.items() if not ok]
            if missing:
                note += f"；缺少依赖：{'、'.join(missing)}"
        return True, note

    def model_path(self, ext_id: str) -> str:
        return self._models.get(ext_id, "")

    def set_model_path(self, ext_id: str, path: str) -> None:
        self._models[ext_id] = path
        settings.set("live_asr_model", path)
        settings.save()

    def shutdown(self) -> None:
        self.stop_all()
        self.manager.cleanup_leftovers()
