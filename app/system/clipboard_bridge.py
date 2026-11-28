"""剪贴板桥：把「划词取词」和「输入框原地替换」做稳。

Windows 上想拿到别的程序里选中的文字，唯一通用的办法是模拟 ``Ctrl+C``；想写回去
只能模拟 ``Ctrl+V``。这条路有三个经典暗坑，这里逐个处理：

1. **修饰键残留**：用户按 ``Alt+Q`` 触发时手指还按着 Alt，此时直接发 Ctrl+C 会被
   目标程序识别成 ``Ctrl+Alt+C``。所以发键之前先把按住不放的修饰键补发 keyup。
2. **不要盲等固定时长**：改用 ``GetClipboardSequenceNumber()`` 轮询——快的程序
   15 ms 就能取到，Electron 应用（VS Code / 浏览器）给到 250 ms 上限也够。
3. **无损还原剪贴板**：取词前备份文本 / HTML / 图片，取完放回去；用户原本复制的
   东西不会凭空消失。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field

from PyQt6.QtCore import QMimeData
from PyQt6.QtGui import QGuiApplication, QImage

VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_SHIFT = 0x10
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_C = 0x43
VK_V = 0x56
VK_A = 0x41

KEYEVENTF_KEYUP = 0x0002

_MODIFIER_KEYS = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_user32 = ctypes.WinDLL("user32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None


@dataclass
class ClipboardSnapshot:
    """剪贴板快照，只保留可无损还原的常见类型。"""

    text: str = ""
    html: str = ""
    image: QImage | None = field(default=None)
    empty: bool = True

    def restore(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if self.image is not None and not self.image.isNull():
            clipboard.setImage(self.image)
            _flush_clipboard()
            return
        if self.html:
            fresh = QMimeData()
            fresh.setHtml(self.html)
            if self.text:
                fresh.setText(self.text)
            clipboard.setMimeData(fresh)
            _flush_clipboard()
            return
        if not self.empty:
            _write_text(self.text or "")


def snapshot_clipboard() -> ClipboardSnapshot:
    clipboard = QGuiApplication.clipboard()
    snap = ClipboardSnapshot()
    try:
        mime = clipboard.mimeData()
        if mime is not None:
            if mime.hasHtml():
                snap.html = mime.html()
            if mime.hasText():
                snap.text = mime.text()
            snap.empty = not (mime.hasHtml() or mime.hasText() or mime.hasImage())
        image = clipboard.image()
        if image is not None and not image.isNull():
            snap.image = image
            snap.empty = False
    except Exception:
        pass
    return snap


def _flush_clipboard() -> None:
    """Qt 在 Windows 上通过 OLE 写剪贴板，需要转一下事件循环才真正落地。"""
    app = QGuiApplication.instance()
    if app is not None:
        app.processEvents()
        app.processEvents()


def _pump(seconds: float) -> None:
    """在等待期间持续跑事件循环。

    目标程序如果是**本进程**里的控件（测试、嵌入式编辑框），``WM_PASTE`` 会排在我们
    自己的消息队列里；只要主线程 ``sleep``，这条消息就一直不被处理，粘贴看起来
    「没反应」。所以等待一律用「processEvents + 短睡」的循环，而不是 ``time.sleep``。
    """
    app = QGuiApplication.instance()
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if app is not None and not app.closingDown():
            app.processEvents()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.015, remaining))


def clipboard_text() -> str:
    """当前剪贴板里的纯文本（监听模式用，不做任何模拟）。"""
    try:
        return (QGuiApplication.clipboard().text() or "").strip()
    except Exception:
        return ""


def _write_text(text: str, *, attempts: int = 6) -> bool:
    """写文本并回读确认。

    剪贴板是全局独占资源：剪贴板管理器、浏览器、远程桌面都可能正占着它，此时
    ``OpenClipboard`` 失败，Qt 只在 stderr 抱怨一句就静默丢弃。所以写一次不够，
    要回读确认并重试，否则「还原剪贴板」会悄无声息地清空用户内容。
    """
    clipboard = QGuiApplication.clipboard()
    for index in range(max(1, attempts)):
        try:
            clipboard.setText(text)
        except Exception:
            pass
        _flush_clipboard()
        if (clipboard.text() or "") == text:
            return True
        time.sleep(0.02 + 0.02 * index)
    return False


# --------------------------------------------------------------------- 按键
def held_modifiers() -> list[int]:
    if _user32 is None:
        return []
    down: list[int] = []
    for vk in _MODIFIER_KEYS:
        try:
            if _user32.GetAsyncKeyState(vk) & 0x8000:
                down.append(vk)
        except Exception:
            continue
    return down


def release_modifiers() -> None:
    """把当前按住不放的修饰键松开，避免 Ctrl+C 被识别成 Ctrl+Alt+C。"""
    if _user32 is None:
        return
    for vk in held_modifiers():
        try:
            _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            continue
    if held_modifiers():
        time.sleep(0.02)


def send_chord(vk: int, *, modifiers: tuple[int, ...] = (VK_CONTROL,)) -> None:
    """发送一个「修饰键 + 主键」组合。"""
    if _user32 is None:
        return
    for mod in modifiers:
        _user32.keybd_event(mod, 0, 0, 0)
    time.sleep(0.02)
    _user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    for mod in reversed(modifiers):
        time.sleep(0.02)
        _user32.keybd_event(mod, 0, KEYEVENTF_KEYUP, 0)


def _sequence_number() -> int:
    if _user32 is None:
        return 0
    try:
        return int(_user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


# --------------------------------------------------------------------- 取词
def _wait_for_change(before: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + max(0.05, timeout_sec)
    while time.monotonic() < deadline:
        if _sequence_number() != before:
            return True
        time.sleep(0.015)
    return False


def capture_selected_text(timeout_sec: float = 0.28, *, restore: bool = True) -> str:
    """模拟 Ctrl+C 取走当前选区，返回文字；取不到返回空串。

    ``restore=True`` 时会把调用前的剪贴板内容放回去（默认行为，用户无感）。
    """
    clipboard = QGuiApplication.clipboard()
    backup = snapshot_clipboard() if restore else None

    try:
        release_modifiers()
        before = _sequence_number()
        send_chord(VK_C)
        _flush_clipboard()
        if not _wait_for_change(before, timeout_sec):
            return ""
        _flush_clipboard()
        return (clipboard.text() or "").strip()
    except Exception:
        return ""
    finally:
        if backup is not None:
            try:
                backup.restore()
                _flush_clipboard()
            except Exception:
                pass


def capture_for_replace(timeout_sec: float = 0.28) -> str:
    """原地替换用：先试选区，取不到就 Ctrl+A 全选后再取一次。"""
    text = capture_selected_text(timeout_sec)
    if text:
        return text
    try:
        release_modifiers()
        before = _sequence_number()
        send_chord(VK_A)
        time.sleep(0.03)
        send_chord(VK_C)
        _flush_clipboard()
        if not _wait_for_change(before, timeout_sec):
            # Ctrl+A 之后再 Ctrl+C 不一定改序号，直接读一次兜底
            _flush_clipboard()
            return (QGuiApplication.clipboard().text() or "").strip()
        _flush_clipboard()
        return (QGuiApplication.clipboard().text() or "").strip()
    except Exception:
        return ""
    finally:
        # 全选会留下选区，撤销掉，避免目标程序里出现一段被选中的文字
        try:
            release_modifiers()
        except Exception:
            pass


def replace_selection_with_text(
    text: str, *, restore_delay: float = 0.45, restore: bool = True
) -> bool:
    """把译文写进剪贴板并模拟 Ctrl+V，覆盖目标程序里当前选区。

    两个细节决定这条路径能不能用：

    * 粘贴之后**必须继续跑事件循环**：目标如果是本进程里的控件，``WM_PASTE``
      排在自己的队列里，主线程一 ``sleep`` 就永远不处理（看起来「没反应」）。
    * **恢复剪贴板要晚一点、且只在剪贴板还是我们写的内容时恢复**：浏览器、
      Electron 程序读剪贴板是异步的，0.12 秒就还原会把用户原来的内容粘进去；
      而如果这期间用户自己复制了别的东西，就不该再覆盖回去。
    """
    if not text:
        return False
    clipboard = QGuiApplication.clipboard()
    backup = snapshot_clipboard() if restore else None
    try:
        release_modifiers()
        if not _write_text(text):
            return False
        send_chord(VK_V)
        _pump(max(0.1, restore_delay))
        return True
    except Exception:
        return False
    finally:
        if backup is not None:
            try:
                if (clipboard.text() or "") == text:
                    backup.restore()
                    _flush_clipboard()
            except Exception:
                pass


# ----------------------------------------------------------------- 前台进程
def foreground_hwnd() -> int:
    """当前前台窗口句柄（用来确认「粘贴目标」没在翻译期间被换掉）。"""
    if _user32 is None:
        return 0
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def foreground_process_name() -> str:
    """当前前台窗口所属进程的可执行文件名（小写，含 .exe）。"""
    if _user32 is None or _kernel32 is None:
        return ""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value.rsplit("\\", 1)[-1].lower()
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        return ""


def is_blocked(names) -> bool:
    """前台程序是否在黑名单里（这些程序里模拟按键不可靠或有害）。"""
    if not names:
        return False
    current = foreground_process_name()
    if not current:
        return False
    blocked = {str(name).strip().lower() for name in names if str(name).strip()}
    return current in blocked
