"""Windows 开机自启：写入当前用户的 ``Run`` 注册表项。

只动 ``HKCU``（不需要管理员权限，也不会影响其他用户）。自启命令带
``--silent``，这样登录后程序直接进托盘并把本地代理拉起来，不弹主窗口。
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "IndexTranslateDesktop"
SILENT_FLAG = "--silent"


class AutostartError(RuntimeError):
    """注册表读写失败时抛出，消息可直接展示给用户。"""


def run_command() -> str:
    """自启时要执行的命令行（已加引号，可直接写进注册表）。"""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{exe}" {SILENT_FLAG}'

    # 源码运行时用 pythonw，避免登录后闪出一个黑色控制台窗口。
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.exists():
        exe = pythonw
    entry = Path(__file__).resolve().parents[2] / "run.py"
    return f'"{exe}" "{entry}" {SILENT_FLAG}'


def _winreg():
    try:
        import winreg  # noqa: PLC0415 - 仅 Windows 有
    except ImportError as exc:  # pragma: no cover - 非 Windows 平台
        raise AutostartError("当前系统不支持开机自启（仅 Windows）。") from exc
    return winreg


def current_value() -> str:
    """注册表里已设置的自启命令；未设置返回空串。"""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value or "")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise AutostartError(f"读取开机自启设置失败：{exc}") from exc


def is_enabled() -> bool:
    try:
        return bool(current_value().strip())
    except AutostartError:
        return False


def enable() -> str:
    """写入自启项，返回实际写入的命令。"""
    winreg = _winreg()
    command = run_command()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError as exc:
        raise AutostartError(f"写入开机自启失败：{exc}") from exc
    return command


def disable() -> None:
    winreg = _winreg()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AutostartError(f"关闭开机自启失败：{exc}") from exc


def set_enabled(value: bool) -> None:
    if value:
        enable()
    else:
        disable()
