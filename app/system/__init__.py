"""Windows 系统级能力的隔离层。

这一层只放「跟操作系统打交道」的代码，不引用任何界面控件，方便单独测试：

* :mod:`app.system.hotkeys`          —— 全局热键（``RegisterHotKey`` + Qt 原生事件过滤）
* :mod:`app.system.clipboard_bridge` —— 划词取词 / 原地替换（剪贴板 + 模拟按键）
* :mod:`app.system.single_instance`  —— 单实例守护（本地命名管道）

之所以不写在 ``app/ui`` 里，是因为 Win32 调用一旦混进窗口代码，后期既难维护
也没法做单元测试。
"""

from __future__ import annotations

__all__ = ["hotkeys", "clipboard_bridge", "single_instance"]
