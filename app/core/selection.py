"""取词：备份剪贴板 → 模拟 Ctrl+C → 读回 → 还原。

划词翻译只能通过「模拟复制」拿到其他程序里选中的文字。直接覆盖用户剪贴板
很失礼，所以这里先把原来的内容（文本 / HTML / 图片）拍个快照，取完再放回去。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from PyQt6.QtGui import QGuiApplication, QImage

# 虚拟键码
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_SHIFT = 0x10
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C
_VK_C = 0x43

_KEYEVENTF_KEYUP = 0x0002


@dataclass
class ClipboardSnapshot:
    """剪贴板快照，只保留可还原的常见类型。"""

    text: str = ""
    html: str = ""
    image: QImage | None = field(default=None)

    def restore(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if self.image is not None and not self.image.isNull():
            clipboard.setImage(self.image)
            return
        if self.html:
            mime = clipboard.mimeData()
            # QMimeData 属于剪贴板，不能直接复用；新建一份再写入。
            from PyQt6.QtCore import QMimeData

            fresh = QMimeData()
            fresh.setHtml(self.html)
            if self.text:
                fresh.setText(self.text)
            clipboard.setMimeData(fresh)
            del mime
            return
        clipboard.setText(self.text or "")


def _release_modifiers() -> None:
    """热键触发时用户还按着 Ctrl/Alt，先松开再发 Ctrl+C，否则会变成组合键。"""
    try:
        import win32api
    except ImportError:
        return
    for vk in (_VK_CONTROL, _VK_MENU, _VK_SHIFT, _VK_LWIN, _VK_RWIN):
        try:
            win32api.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        except Exception:
            continue


def _send_copy() -> None:
    import win32api

    win32api.keybd_event(_VK_CONTROL, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(_VK_C, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(_VK_C, 0, _KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)
    win32api.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)


def snapshot_clipboard() -> ClipboardSnapshot:
    clipboard = QGuiApplication.clipboard()
    mime = clipboard.mimeData()
    snap = ClipboardSnapshot()
    try:
        if mime is not None:
            if mime.hasHtml():
                snap.html = mime.html()
            if mime.hasText():
                snap.text = mime.text()
        image = clipboard.image()
        if image is not None and not image.isNull():
            snap.image = image
    except Exception:
        pass
    return snap


def copy_selection(timeout_ms: int = 900) -> str:
    """模拟复制并返回选中的文字；取不到返回空串。

    用哨兵字符串判断剪贴板是否真的变了，避免「选中的内容刚好等于原剪贴板」
    时白等一整轮超时。
    """
    clipboard = QGuiApplication.clipboard()
    backup = snapshot_clipboard()
    sentinel = f"\u200b__INDEX_TRANSLATE_{uuid.uuid4().hex}__"

    try:
        _release_modifiers()
        clipboard.setText(sentinel)
        _send_copy()

        deadline = time.monotonic() + max(0.2, timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            QGuiApplication.processEvents()
            text = clipboard.text()
            if text and text != sentinel:
                return text.strip()
            time.sleep(0.03)
        return ""
    except Exception:
        return ""
    finally:
        try:
            backup.restore()
        except Exception:
            pass


def clipboard_text() -> str:
    """当前剪贴板里的纯文本（用于监听模式，不做任何模拟）。"""
    try:
        return (QGuiApplication.clipboard().text() or "").strip()
    except Exception:
        return ""
