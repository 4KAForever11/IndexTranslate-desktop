"""把「本地 OCR 扩展」的调用包成 QThread。

OCR 是 CPU 密集的（PP-OCRv4 单张 1080p 大约 0.3–1.2 秒），直接放主线程会卡住
整个界面；而 Sidecar 的管道读写本身是线程安全的，所以这里只在后台线程里
发起一次 ``ocr`` 调用，把结果通过信号送回主线程。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

from ..core.config import data_dir
from ..core.extensions.sidecar_client import SidecarClient, SidecarError

OCR_ID = "ext-ocr-overlay"


class OcrTask(QThread):
    """后台执行一次本地 OCR。"""

    done = pyqtSignal(object, object)  # (QImage, list[dict])
    failed = pyqtSignal(str)

    def __init__(self, client: SidecarClient, image: QImage, parent=None) -> None:
        super().__init__(parent)
        self._client = client
        self._image = image

    def run(self) -> None:
        handle, raw_path = tempfile.mkstemp(prefix="ocr-", suffix=".png", dir=str(data_dir()))
        os.close(handle)
        path = Path(raw_path)
        try:
            if not self._image.save(str(path), "PNG"):
                self.failed.emit("截图保存失败，无法识别。")
                return
            try:
                result = self._client.call("ocr", {"image": str(path)}, timeout=120.0)
            except SidecarError as exc:
                self.failed.emit(str(exc))
                return
            except Exception as exc:  # 扩展协议异常也只报一句
                self.failed.emit(f"本地 OCR 出错：{exc}")
                return
            segments = result.get("segments") if isinstance(result, dict) else None
            self.done.emit(self._image, list(segments or []))
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass