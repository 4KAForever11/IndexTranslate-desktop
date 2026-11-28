"""全局热键：Win32 ``RegisterHotKey`` + Qt 原生事件过滤。

为什么不用 ``keyboard`` 之类的钩子库：``SetWindowsHookEx`` 属于全局键盘钩子，
国内杀软几乎必报毒，打包后兼容性也差。``RegisterHotKey`` 是系统给普通应用的
正规接口，零误报。

为什么不用「独立线程 + GetMessage」：那样回调落在自建线程里，还得手动跨线程
投递信号；改成 QAbstractNativeEventFilter 后，``WM_HOTKEY`` 直接在主线程的事件
循环里被拦截，回调天然在 GUI 线程，不会和界面状态打架。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Iterable

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # 按住不放时只触发一次

ERROR_HOTKEY_ALREADY_REGISTERED = 1409
ERROR_ACCESS_DENIED = 5

_MODIFIER_NAMES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "meta": MOD_WIN,
    "cmd": MOD_WIN,
}

# 只支持常见按键：够用，而且避免 VkKeyScan 在中文输入法下拿到错误码点。
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

# 用 use_last_error=True 才能可靠拿到 RegisterHotKey 失败原因。
_user32 = ctypes.WinDLL("user32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None


class HotkeyError(RuntimeError):
    """注册失败，消息可直接展示给用户。"""


def _describe_error(sequence: str, code: int | None) -> str:
    if code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return f"快捷键 {sequence} 已被其他程序占用，换一个组合试试。"
    if code == ERROR_ACCESS_DENIED:
        return f"没有权限注册快捷键 {sequence}，请尝试以管理员身份运行。"
    if code:
        return f"全局热键 {sequence} 注册失败（错误码 {code}）。"
    return f"全局热键 {sequence} 注册失败。"


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


class _HotkeyFilter(QAbstractNativeEventFilter):
    """真正的原生事件过滤器。

    PyQt 里「QObject + QAbstractNativeEventFilter」多继承时，虚函数分派会落到
    QObject 那一侧，``nativeEventFilter`` 根本不会被调用（踩过一次：消息发出去了，
    回调静默不触发）。所以把过滤器拆成独立的 QAbstractNativeEventFilter 子类，
    只负责转发给管理器。
    """

    def __init__(self, manager: "HotkeyManager") -> None:
        super().__init__()
        self._manager = manager

    def nativeEventFilter(self, event_type, message):  # noqa: N802, ANN001
        return self._manager.handle_native(event_type, message)


class HotkeyManager(QObject):
    """管理一组全局热键，触发时发出动作名。

    用法::

        manager = HotkeyManager(app)
        manager.triggered.connect(on_action)          # on_action("capture")
        manager.register("capture", "Ctrl+Alt+S")
        manager.install()
    """

    triggered = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None, *, base_id: int = 0xB100) -> None:
        super().__init__(parent)
        self._base_id = int(base_id)
        self._next_offset = 0
        self._id_to_action: dict[int, str] = {}
        self._action_state: dict[str, tuple[int, int, str]] = {}  # action -> (id, mods, vk)
        self._installed = False
        self._filter = _HotkeyFilter(self)

    # ------------------------------------------------------------------ state
    @property
    def actions(self) -> list[str]:
        return list(self._action_state)

    def is_active(self, action: str) -> bool:
        return action in self._action_state

    def sequence_of(self, action: str) -> str:
        return self._action_state.get(action, (0, 0, ""))[2]

    # ---------------------------------------------------------------- control
    def install(self, app=None) -> None:
        """把事件过滤器挂到 QApplication 上（重复调用无副作用）。"""
        if self._installed:
            return
        if app is None:
            from PyQt6.QtWidgets import QApplication

            app = QApplication.instance()
        if app is None:
            raise HotkeyError("热键管理器需要在 QApplication 之后初始化。")
        app.installNativeEventFilter(self._filter)
        self._installed = True

    def register(self, action: str, sequence: str) -> bool:
        """注册一个动作的热键（会先注销同名动作）。失败抛 :class:`HotkeyError`。"""
        if _user32 is None:  # pragma: no cover - 非 Windows
            raise HotkeyError("全局热键仅支持 Windows。")
        self.unregister(action)

        modifiers, vk = parse_sequence(sequence)
        hotkey_id = self._base_id + self._next_offset
        # 绑到当前线程的消息队列：不依赖任何窗口句柄，窗口隐藏时也能收到。
        if not _user32.RegisterHotKey(None, hotkey_id, modifiers | MOD_NOREPEAT, vk):
            code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else None
            raise HotkeyError(_describe_error(sequence, code))

        self._next_offset += 1
        self._id_to_action[hotkey_id] = action
        self._action_state[action] = (hotkey_id, modifiers, sequence)
        return True

    def unregister(self, action: str) -> None:
        state = self._action_state.pop(action, None)
        if state is None:
            return
        hotkey_id = state[0]
        self._id_to_action.pop(hotkey_id, None)
        if _user32 is not None:
            try:
                _user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                pass

    def unregister_all(self) -> None:
        for action in list(self._action_state):
            self.unregister(action)
        self._next_offset = 0

    def register_many(self, bindings: Iterable[tuple[str, str]]) -> dict[str, str]:
        """批量注册，返回 ``{action: 错误信息}``（成功的动作不在结果里）。"""
        errors: dict[str, str] = {}
        for action, sequence in bindings:
            try:
                self.register(action, sequence)
            except HotkeyError as exc:
                errors[action] = str(exc)
        return errors

    # ------------------------------------------------------------ event filter
    def handle_native(self, event_type, message):  # noqa: ANN001
        """解析原生消息；命中自家热键时发 ``triggered``。

        PyQt6 传进来的 ``event_type`` 是 ``QByteArray``，不是 ``bytes``——必须先
        ``bytes()`` 再 decode，否则拿到的永远不是 ``windows_generic_MSG``。
        """
        try:
            raw = event_type.data() if hasattr(event_type, "data") else event_type
            name = raw.decode("ascii", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:
            name = ""
        if name not in ("windows_generic_MSG", "windows_dispatcher_MSG"):
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY:
            action = self._id_to_action.get(int(msg.wParam))
            if action:
                self.triggered.emit(action)
                return True, 0
        return False, 0
