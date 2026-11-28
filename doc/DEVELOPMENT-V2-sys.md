底座重构完成后，下一步投资回报率最高的是做 **「系统级常驻与无切窗交互层」**：系统托盘常驻、全局热键划词浮窗、内置框选截屏翻译，顺手带出输入框原地替换。

选这一步作为第二步有三个直接理由：

1. **零新增重型依赖**：你仓库里已经有 `PyQt6`、`PyQt6-Fluent-Widgets`、`Pillow` 和 `pywin32`，这一整套功能打完包体积几乎不涨。
2. **立刻检验新底座**：主窗口、划词浮窗、框选截屏、本地代理四个入口同时接入你的 `TranslationPipeline`，能第一时间暴露并发、取消信号或状态耦合问题。
3. **补齐体验闭环**：你之前已经做了沉浸式翻译本地代理，没有托盘常驻的话用户随手关窗代理就断了；加上托盘和全局热键后，软件才真正从「需要切窗口的工具」变成「随时呼出的系统级助手」。

## 一、目录结构增量设计

把所有 Windows 底层操作收口到独立的 `app/system/`，不要把 Win32 API 写进 UI 文件里，避免后期维护和单元测试困难：

app/
├─ system/                   # [新增] Windows 系统级能力隔离层
│  ├─ __init__.py
│  ├─ hotkeys.py             # WM_HOTKEY 全局热键注册 + Qt 原生事件过滤
│  ├─ clipboard_bridge.py    # 安全的剪贴板备份/划词取词/原地粘贴回写
│  └─ single_instance.py     # 单实例守护（双击 exe 唤醒托盘窗口而非多开）
├─ ui/
│  ├─ tray.py                # [新增] 系统托盘图标与右键菜单
│  ├─ popup_window.py        # [新增] 无边框流式划词浮窗（支持置顶/转主窗）
│  ├─ capture_overlay.py     # [新增] 多屏高 DPI 框选截屏遮罩
│  └─ main_window.py         # [小改] 拦截 closeEvent 最小化到托盘、接入全局信号
└─ core/
   └─ config.py              # [小改] 增加热键绑定、关闭到托盘、黑名单进程配置



## 二、分模块开发方案与避坑要点

### 模块 1：全局热键管理器（`app/system/hotkeys.py`）

**关键选型**：千万不要用 `keyboard` 库的底层钩子（`SetWindowsHookEx`），国内杀软极易报毒，且打包后兼容性差。直接用 Win32 原生 `RegisterHotKey`，配合 PyQt6 的 `QAbstractNativeEventFilter` 监听 `WM_HOTKEY`，回调天然落在 Qt 主线程，稳定且零误报。

from __future__ import annotations
import ctypes
from ctypes import wintypes
from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # 防止按住不放重复触发

user32 = ctypes.windll.user32


class HotkeyManager(QObject, QAbstractNativeEventFilter):
    """基于 Win32 RegisterHotKey 的全局热键管理器"""

```python
triggered = pyqtSignal(str)  # 发射动作名，如 "translate_selection" / "capture_screen"

def __init__(self, parent=None):
    QObject.__init__(self, parent)
    QAbstractNativeEventFilter.__init__(self)
    self._id_to_action: dict[int, str] = {}
    self._next_id = 1

def register(self, action: str, modifiers: int, vk_code: int) -> bool:
    hotkey_id = self._next_id
    # 传 None 绑定到当前线程消息队列，无需依赖某个特定窗口句柄
    ok = bool(user32.RegisterHotKey(None, hotkey_id, modifiers | MOD_NOREPEAT, vk_code))
    if ok:
        self._id_to_action[hotkey_id] = action
        self._next_id += 1
    return ok

def unregister_all(self) -> None:
    for hotkey_id in list(self._id_to_action.keys()):
        user32.UnregisterHotKey(None, hotkey_id)
    self._id_to_action.clear()

def nativeEventFilter(self, event_type, message):
    if event_type == b"windows_generic_MSG":
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY:
            action = self._id_to_action.get(msg.wParam)
            if action:
                self.triggered.emit(action)
                return True, 0
    return False, 0
```



在 `app/main.py` 启动时调用 `app.installNativeEventFilter(hotkey_mgr)`，退出前调用 `hotkey_mgr.unregister_all()`。如果某热键返回 `False`，说明被微信/QQ/IDE 占用，直接在设置页标红提示冲突即可。

### 模块 2：防踩坑的划词取词与原地替换（`app/system/clipboard_bridge.py`）

Windows 模拟 `Ctrl+C` 划词有**三个经典暗坑**，处理好这三个点，体验就稳过一半桌面翻译软件：

1. **修饰键残留**：比如用户按 `Alt+Q` 触发划词时，手指还没松开 `Alt`，此时直接发 `Ctrl+C` 实际会被目标软件识别成 `Ctrl+Alt+C` 导致复制失败。必须在发模拟按键前**先对所有按下的修饰键补发 `KEYEVENTF_KEYUP`**（或短等抬起）。
2. **不要盲目 `sleep(0.2)`**：改用 Win32 `GetClipboardSequenceNumber()` 轮询序号变化——快的应用 15ms 就能取到词，慢的 Electron 应用（如 VS Code/浏览器）给足 250ms 超时上限。
3. **无损备份与恢复**：只备份纯文本并在取词后异步恢复，避免覆盖用户原本复制的内容；如果用户原剪贴板里是图片或文件列表，至少先判断再决定是否覆盖。

from __future__ import annotations
import ctypes
import time
import win32api
import win32con
from PyQt6.QtWidgets import QApplication

user32 = ctypes.windll.user32
VK_MODIFIERS = (win32con.VK_CONTROL, win32con.VK_MENU, win32con.VK_SHIFT, win32con.VK_LWIN, win32con.VK_RWIN)


def _release_modifiers() -> None:
    for vk in VK_MODIFIERS:
        if win32api.GetAsyncKeyState(vk) & 0x8000:
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _send_chord(vk_key: int) -> None:
    """发送 Ctrl + vk_key"""
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(vk_key, 0, 0, 0)
    win32api.keybd_event(vk_key, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def capture_selected_text(timeout_sec: float = 0.28) -> str:
    clipboard = QApplication.clipboard()
    # 仅做文本级备份（如需完整备份可用 win32clipboard 缓存 CF_UNICODETEXT）
    old_text = clipboard.text() if clipboard.mimeData().hasText() else None

    seq_before = user32.GetClipboardSequenceNumber()
    _release_modifiers()
    time.sleep(0.02)
    _send_chord(ord("C"))
    
    deadline = time.monotonic() + timeout_sec
    changed = False
    while time.monotonic() < deadline:
        if user32.GetClipboardSequenceNumber() != seq_before:
            changed = True
            break
        time.sleep(0.015)
    
    selected = clipboard.text().strip() if changed else ""
    
    if old_text is not None:
        clipboard.setText(old_text)
    return selected

def replace_selection_with_text(translated: str) -> None:
    """原地替换：将译文写入剪贴板并模拟 Ctrl+V"""
    clipboard = QApplication.clipboard()
    old_text = clipboard.text() if clipboard.mimeData().hasText() else None
    clipboard.setText(translated)
    time.sleep(0.02)
    _release_modifiers()
    _send_chord(ord("V"))
    #给目标程序留出读取剪贴板的时间再还原
    time.sleep(0.12)
    if old_text is not None:
        clipboard.setText(old_text)



**顺手白捡「输入框原地替换」**：复用上面两个函数，再注册一个热键（如 `Alt+W`）：捕获选中文本（若未选中可先模拟一次 `Ctrl+A`）→ 调 `TranslationPipeline`（默认走极速 `2B` 或 `9B` 模型，缩短等待）→ 拿到完整译文后调用 `replace_selection_with_text()`。总共不到 30 行代码，写英文邮件、GitHub Issue 和外网聊天极其顺手。

### 模块 3：无边框流式划词浮窗（`app/ui/popup_window.py`）

设计重点是**轻、快、不挡视线**：

- **窗口属性**：`Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint`（用 `Tool` 标志不会在 Windows 任务栏多出一个图标）。
- **失焦自动隐藏 + 图钉锁定**：默认点击外部区域（`QEvent.Type.WindowDeactivate`）自动隐藏并调用 `cancel_event.set()` 中断未完成的流式请求；点击右上角「图钉（Pin）」后锁定常驻。
- **多屏边界防溢出**：弹窗默认出现在 `QCursor.pos()` 右下方偏移 14px，必须用 `QGuiApplication.screenAt(cursor_pos).availableGeometry()` 做边缘钳制，防止光标靠近屏幕右边或底边时弹窗露到屏外。
- **直接复用 `TranslationPipeline`**：顶部只放极简工具栏（源/目标语言切换、模型微标、停止/复制/图钉/「在主窗口打开」按钮），正文区流式渲染译文；命中缓存或触发降级时，底部小字提示 `缓存命中` 或 `已降级至 9B`。



### 模块 4：内置高 DPI 多屏框选截屏（`app/ui/capture_overlay.py`）

Windows 多显示器混合缩放（比如主屏 150%、副屏 100%）是截屏功能最容易画糊、偏移的雷区。**最稳妥的做法是「按当前光标所在屏幕独立铺满遮罩」，而不是强拼整张虚拟桌面大图**：

1. 触发截屏热键（如 `Alt+A`）时，用 `screen = QGuiApplication.screenAt(QCursor.pos())` 获取当前屏幕。
2. 立刻 `pixmap = screen.grabWindow(0)` 冻结该屏幕画面，同时记录 `dpr = pixmap.devicePixelRatio()`。
3. 弹出一个无边框置顶全屏遮罩窗口，设置几何范围为 `screen.geometry()`，在 `paintEvent` 里先画冻结底图，再叠一层半透明黑色蒙版（`rgba(0, 0, 0, 110)`），拖拽选区内部挖空显示原图亮度，并在选区角落实时显示宽高像素。
4. 松开鼠标左键时，把逻辑选区 `QRect` 乘以 `dpr` 裁剪 `pixmap`（保证高分屏裁出的是物理原生清晰度，不被模糊缩放），转为 PNG bytes 后直接丢进你原有的 `image_utils.py` 压缩转 Base64，然后交给 `TranslationPipeline`（结果展示在浮窗或拉起主窗口，由用户设置决定）。
5. 按 `Esc` 或右键单击直接取消。



### 模块 5：系统托盘常驻与单实例守护（`app/ui/tray.py` & `single_instance.py`）

- **关闭行为改造**：在 `MainWindow.closeEvent` 中检查配置 `close_to_tray`；为真时执行 `event.ignore(); self.hide()`，并在首次最小化时弹一条托盘气泡提示（之后不再打扰）。
- **托盘右键菜单**：
  - 显示主窗口（双击托盘图标同样触发）
  - 框选截屏翻译
  - 本地代理开关（动态显示状态与端口，如 `本地代理：运行中 (127.0.0.1:8080)`）
  - 剪贴板监听开关（可选顺手加：监听 `QApplication.clipboard().dataChanged`，配合主窗置顶看 PDF / 游戏文本极爽）
  - 退出程序（真正释放热键、停止代理线程、刷盘 SQLite 后 `QApplication.quit()`）
- **单实例守护**：用 `QLocalServer` + `QLocalSocket`（PyQt6 自带，无需额外依赖）监听固定命名管道 `IndexTranslateDesktopInstance`。用户再次双击桌面 exe 时，新进程向管道发一条 `"SHOW"` 指令后立刻退出，旧进程收到后将主窗口从托盘唤醒置前。
- **开机静默自启（可选）**：写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，传入 `--minimized` 启动参数，开机后直接缩在托盘并拉起本地代理，不弹主窗打扰。



三、开发顺序与验收清单

建议按下面 4 个小闭环逐个合入，每做完一步就能真实用起来：

1. **闭环 A（半天）**：`tray.py` + `single_instance.py` + `MainWindow` 最小化到托盘与 `--minimized` 参数。
2. **闭环 B（1 天）**：`hotkeys.py` + `clipboard_bridge.py` + `popup_window.py`，跑通「任意软件划词 → 按热键 → 浮窗流式出译文 → 失焦隐藏与中断」。
3. **闭环 C（半天）**：`capture_overlay.py` 接入 `image_utils.py` 和 `TranslationPipeline`，跑通「按热键 → 框选 → 直接出图译结果」。
4. **闭环 D（2 小时）**：基于闭环 B 顺手补齐「输入框原地替换」热键和「设置页热键自定义 + 冲突检测」。

**必须实测的 6 个边界场景**：

| **验收场景**                            | **预期表现**                                                 |
| --------------------------------------- | ------------------------------------------------------------ |
| **双屏不同缩放（如 150% + 100%）**      | 在副屏触发截屏，选区坐标与裁剪内容 1:1 对齐，无模糊与偏移；浮窗不出屏 |
| **高频率连按热键 / 翻译中途切走窗口**   | 上一次 `TranslationPipeline` 请求被 `cancel_event` 干净取消，不出现旧译文串进新窗口 |
| **在浏览器 / VS Code / 记事本划词**     | 连续 10 次触发均能稳定取到选中词，且用户原先的剪贴板文本不被覆盖 |
| **热键被占用（如与微信 `Alt+A` 冲突）** | 启动不崩溃，设置页明确提示占用状态，允许用户改绑             |
| **托盘常驻下重启沉浸式翻译请求**        | 关闭主窗口后代理端口继续响应，双击 exe 只唤醒旧窗口不多开第二个代理冲突端口 |
| **程序完全退出**                        | 托盘菜单点「退出」后，进程彻底结束无残留，已注册全局热键释放归还系统 |



这一步做完，`IndexTranslate-desktop` 就已经具备了 Bob / Pot 级别的核心日常体验