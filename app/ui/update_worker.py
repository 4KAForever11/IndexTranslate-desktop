"""自动更新：后台检查 + 后台下载，界面线程只负责显示进度。

真正的「等进程退出 → 替换 → 重启」由 `app.core.updater` 生成批处理完成，
这里只负责把三件事串起来，并把结果回报给设置页。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from ..core.config import data_dir
from ..core.updater import Asset, ReleaseInfo, UpdateError, UpdateStatus, Updater


def install_kind() -> str:
    """这套程序是怎么装的：onefile / folder / source。

    PyInstaller 的目录版在 exe 旁边有 `_internal`，单文件版没有；
    源码运行时 `sys.frozen` 不存在，自动替换会把仓库改坏，所以直接拒绝。
    """
    if not getattr(sys, "frozen", False):
        return "source"
    exe = Path(sys.executable)
    if (exe.parent / "_internal").is_dir():
        return "folder"
    return "onefile"


def pick_asset(release: ReleaseInfo, kind: str | None = None) -> Asset | None:
    kind = kind or install_kind()
    if kind == "onefile":
        return release.onefile_asset() or release.folder_asset()
    if kind == "folder":
        return release.folder_asset() or release.onefile_asset()
    return release.folder_asset() or release.onefile_asset()


def updates_dir() -> Path:
    folder = data_dir() / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class UpdateCheckWorker(QThread):
    """检查 GitHub Releases 有没有新版本。"""

    checked = pyqtSignal(object)  # UpdateStatus
    failed = pyqtSignal(str)

    def __init__(self, *, channel: str = "stable", force: bool = True, parent: Any = None) -> None:
        super().__init__(parent)
        self._channel = channel or "stable"
        self._force = force

    def run(self) -> None:  # noqa: D102
        try:
            status = Updater(channel=self._channel).check(force=self._force)
        except UpdateError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"检查更新失败：{exc}")
            return
        self.checked.emit(status)


class UpdateDownloadWorker(QThread):
    """把更新包下载到数据目录，并校验 SHA-256。"""

    progressed = pyqtSignal(int, int)
    ready = pyqtSignal(object, object)  # Path, ReleaseInfo
    failed = pyqtSignal(str)

    def __init__(self, release: ReleaseInfo, *, asset: Asset | None = None, parent: Any = None) -> None:
        super().__init__(parent)
        self._release = release
        self._asset = asset or pick_asset(release)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: D102
        if self._asset is None:
            self.failed.emit("这个版本没有可用的安装包（发布页里缺少 zip / exe）。")
            return

        def progress(done: int, total: int) -> None:
            self.progressed.emit(int(done), int(total))

        try:
            path = Updater().download(
                self._asset, updates_dir(), progress=progress, cancel=self._cancelled_flag()
            )
        except UpdateError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"下载更新失败：{exc}")
            return
        self.ready.emit(path, self._release)

    # ---------------------------------------------------------------- 取消
    def _cancelled_flag(self):
        outer = self

        class _Cancel:
            def is_set(self) -> bool:
                return outer._cancelled

        return _Cancel()


def build_apply_script(downloaded: Path) -> Path:
    """生成「等本次进程退出 → 覆盖 → 重新启动」的批处理。"""
    exe = Path(sys.executable)
    updater = Updater()
    return updater.write_update_script(
        Path(downloaded),
        app_dir=exe.parent,
        exe_name=exe.name,
    )


__all__ = [
    "UpdateCheckWorker",
    "UpdateDownloadWorker",
    "UpdateStatus",
    "build_apply_script",
    "install_kind",
    "pick_asset",
    "updates_dir",
]