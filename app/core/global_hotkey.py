"""全局热键（Windows ``RegisterHotKey``）。

用一条独立线程跑 ``GetMessage`` 循环，把 ``WM_HOTKEY`` 转成 Qt 信号发回主线程。
这样即使主窗口没有焦点、被最小化或已经收进托盘，热键依然有效——这正是
「划词翻译」需要的效果。
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, pyqtSignal

HOTKEY_ID = 0xB17E

# RegisterHotKey 的修饰键位
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

_MODIFIER_NAMES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "meta": MOD_WIN,
}

# 只支持常见按键，够用且不会因为 VkKeyScan 在中文输入法下拿到错误码点。
_NAMED_KEYS = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "\\": 0xDC,
}


class HotkeyError(RuntimeError):
    """注册失败，消息可直接展示给用户。"""


def _register_error(sequence: str, exc: Exception) -> str:
    """把 RegisterHotKey 的 Win32 错误码翻译成用户能照做的提示。"""
    code = getattr(exc, "winerror", None)
    if code is None:
        code = getattr(exc, "args", [None])[0]
    if code == 1409:  # ERROR_HOTKEY_ALREADY_REGISTERED
        return f"快捷键 {sequence} 已被其他程序占用，换一个组合试试。"
    if code == 5:  # ERROR_ACCESS_DENIED
        return f"没有权限注册快捷键 {sequence}，请尝试以管理员身份运行。"
    return f"全局热键 {sequence} 注册失败：{exc}"


def parse_sequence(sequence: str) -> tuple[int, int]:
    """``"Ctrl+Alt+T"`` → ``(modifiers, virtual_key)``。"""
    parts = [p.strip() for p in str(sequence or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("快捷键不能为空。")

    modifiers = 0
    key: int | None = None
    for part in parts:
        lowered = part.lower()
        if lowered in _MODIFIER_NAMES:
            modifiers |= _MODIFIER_NAMES[lowered]
            continue
        if key is not None:
            raise HotkeyError(f"快捷键「{sequence}」只能包含一个主键。")
        if len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif lowered in _NAMED_KEYS:
            key = _NAMED_KEYS[lowered]
        elif lowered.startswith("f") and lowered[1:].isdigit() and 1 <= int(lowered[1:]) <= 24:
            key = 0x70 + int(lowered[1:]) - 1
        else:
            raise HotkeyError(f"无法识别的按键「{part}」。")

    if key is None:
        raise HotkeyError(f"快捷键「{sequence}」缺少主键，例如 Ctrl+Alt+T。")
    if not modifiers:
        raise HotkeyError(f"快捷键「{sequence}」需要至少一个修饰键（Ctrl/Alt/Shift/Win）。")
    return modifiers, key


class GlobalHotkey(QObject):
    """一个全局热键。``register`` 成功后 ``triggered`` 会在主线程发出。"""

    triggered = pyqtSignal()

    def __init__(self, parent: QObject | None = None, hotkey_id: int = HOTKEY_ID) -> None:
        super().__init__(parent)
        self._hotkey_id = int(hotkey_id)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._thread_id = 0
        self._sequence = ""

    # ------------------------------------------------------------------ state
    @property
    def sequence(self) -> str:
        return self._sequence

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------------------------------------------------------------- control
    def register(self, sequence: str) -> None:
        """注册（会先注销旧的）。失败抛 :class:`HotkeyError`。"""
        self.unregister()
        modifiers, key = parse_sequence(sequence)
        try:
            import win32api  # noqa: F401, PLC0415
            import win32con
            import win32gui
        except ImportError as exc:  # pragma: no cover - 非 Windows
            raise HotkeyError(f"全局热键需要 pywin32：{exc}") from exc

        started = threading.Event()
        error: list[str] = []

        def run() -> None:
            self._thread_id = win32api.GetCurrentThreadId()
            # 注意：pywin32 的 RegisterHotKey 成功时返回 None、失败时抛异常，
            # 不能用返回值判断。
            try:
                win32gui.RegisterHotKey(None, self._hotkey_id, modifiers, key)
            except Exception as exc:  # noqa: BLE001 - 需要把系统错误翻译成人话
                error.append(_register_error(sequence, exc))
                started.set()
                return
            started.set()
            try:
                while not self._stop.is_set():
                    try:
                        raw = win32gui.GetMessage(None, 0, 0)
                    except Exception:  # noqa: BLE001 - 线程收尾时的正常异常
                        break
                    if not raw:
                        break
                    # pywin32 返回 (retval, (hwnd, msg, wParam, lParam, time, pt))
                    message = raw[1] if isinstance(raw[1], tuple) else raw
                    if message[1] == win32con.WM_QUIT:
                        break
                    if message[1] == win32con.WM_HOTKEY and message[2] == self._hotkey_id:
                        self.triggered.emit()
            finally:
                try:
                    win32gui.UnregisterHotKey(None, self._hotkey_id)
                except Exception:
                    pass

        self._stop.clear()
        thread = threading.Thread(target=run, name="index-translate-hotkey", daemon=True)
        self._thread = thread
        thread.start()
        started.wait(3.0)
        if error:
            self._thread = None
            raise HotkeyError(error[0])
        self._sequence = sequence

    def unregister(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        try:
            import win32api
            import win32con

            if self._thread_id:
                win32api.PostThreadMessage(self._thread_id, win32con.WM_QUIT, 0, 0)
        except Exception:
            pass
        thread.join(2.0)
        self._thread = None
        self._thread_id = 0
        self._sequence = ""
