"""Application entry point."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

from PyQt6.QtGui import QFontDatabase, QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox
from qfluentwidgets import Theme, setTheme, setThemeColor

from .core import i18n, settings
from .core.config import APP_NAME, APP_SLUG, VERSION, data_dir, resource_path
from .system.single_instance import SingleInstance
from .ui.main_window import MainWindow

ACCENT = "#0078D4"
INSTANCE_KEY = "IndexTranslateDesktopInstance"


def _install_excepthook() -> None:
    """Log crashes to a file and tell the user where it is."""
    log_path = data_dir() / "crash.log"

    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n{text}")
        except Exception:
            pass
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass
        try:
            QMessageBox.critical(
                None,
                "程序出现异常",
                f"发生未预期的错误，详情已写入：\n{log_path}\n\n{exc_type.__name__}: {exc}",
            )
        except Exception:
            pass

    sys.excepthook = hook


def _preferred_font() -> str:
    families = set(QFontDatabase.families())
    for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
        if family in families:
            return family
    return ""


def _selftest_extension(window: MainWindow) -> dict:
    """扩展握手：装了就 ping 一次，没装就说明「未安装」，不视为失败。"""
    runtime = getattr(window, "extensions", None)
    if runtime is None:
        return {"installed": 0, "checked": [], "skipped": "扩展运行时未初始化"}
    rows = runtime.status()
    if not rows:
        return {"installed": 0, "checked": [], "skipped": "未安装任何扩展"}
    checked: list[dict] = []
    for row in rows:
        ext_id = str(row.get("id") or row.get("extension") or "")
        if not row.get("enabled") or not row.get("host_ok", True):
            checked.append({"id": ext_id, "skipped": "未启用或宿主版本不满足"})
            continue
        ok, message = runtime.ping(ext_id)
        checked.append({"id": ext_id, "ok": ok, "message": message})
    return {"installed": len(rows), "checked": checked}


def _selftest_sync() -> dict:
    """WebDAV 连通性；没填地址就跳过，避免自检依赖网络配置。"""
    if not str(settings.get("sync_url", "") or "").strip():
        return {"skipped": "未配置 WebDAV 地址"}
    try:
        from .core.sync.webdav_sync import WebDAVClient
        from .core.secret_store import SecretError, unprotect

        try:
            password = unprotect(str(settings.get("sync_password", "") or ""))
        except SecretError as exc:
            return {"ok": False, "message": str(exc)}
        client = WebDAVClient(
            str(settings.get("sync_url", "")),
            str(settings.get("sync_user", "") or ""),
            password,
        )
        ok, message = client.probe()
        return {"ok": ok, "message": message}
    except Exception as exc:  # noqa: BLE001 - 自检只报告，不抛
        return {"ok": False, "message": repr(exc)}


def _selftest(app: QApplication, window: MainWindow) -> int:
    """Translate one sentence through the real widgets and write a report.

    Used to verify a packaged build end to end: `IndexTranslate-desktop.exe --selftest`
    writes selftest.json into the data directory and exits non-zero on failure.
    """
    page = window.translate_page
    page.reload_settings()
    index = page.tgt_combo.findData("en")
    if index >= 0:
        page.tgt_combo.setCurrentIndex(index)

    def run_once(timeout: float = 120.0) -> str:
        page.start_translation()
        deadline = time.monotonic() + timeout
        while page.is_busy() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        for _ in range(120):
            app.processEvents()
            time.sleep(0.01)
        return page.output.toPlainText().strip()

    page.editor.setPlainText("技术赋能创作，让交流跨越语言的界限。")
    result = run_once()

    image_result = ""
    image_error = ""
    try:
        from PIL import Image, ImageDraw, ImageFont

        shot = Path(tempfile.gettempdir()) / "index_translate_selftest.png"
        canvas = Image.new("RGB", (760, 220), "white")
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 30)
        draw.text((30, 60), "今天的天气很好，我们一起去公园散步吧。", fill=(20, 20, 20), font=font)
        canvas.save(shot)
        page.clear_all()
        page.set_image_from_path(str(shot))
        image_result = run_once(180.0)
    except Exception as exc:  # pillow font, temp file, network — report, do not crash
        image_error = repr(exc)

    extension_result = _selftest_extension(window)
    sync_result = _selftest_sync()

    report = {
        "version": VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "data_dir": str(data_dir()),
        "status": page.status_label.text(),
        "elapsed": page.elapsed_label.text(),
        "result": result,
        "image_result": image_result,
        "extension": extension_result,
        "sync": sync_result,
        # 扩展与同步是可选能力：没装扩展 / 没配 WebDAV 不算失败，只如实记录。
        "ok": bool(result) and bool(image_result),
    }
    try:
        (data_dir() / "selftest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


def _final_exit(window: MainWindow, app: QApplication, code: int) -> None:
    """做完收尾后直接结束进程，跳过解释器的 Qt 析构。

    实测（PyQt6 6.12 + qfluentwidgets）：正常返回时有一半概率在 C++ 侧收尾阶段
    触发 access violation（退出码 0xC0000005，Windows 会记一条应用程序错误）。
    根因是窗口/托盘/原生事件过滤器这些 C++ 对象的析构顺序不受 Python 控制；
    我们自己的清理已经在 _shutdown() 里显式做完（停代理、断扩展子进程、关库、存几何），
    所以这里直接 os._exit，既保证退出码干净，也不会留下需要用户点掉的崩溃对话框。
    """
    try:
        window._persist_geometry()  # noqa: SLF001 - 正常关窗才会走到 closeEvent，这里补一次
    except Exception:
        pass
    try:
        window._shutdown()  # noqa: SLF001 - 幂等，selftest 路径可能还没走过
    except Exception:
        pass
    try:
        if window.tray is not None:
            window.tray.hide()
        window.hide()
        app.processEvents()
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(int(code))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_SLUG)
    app.setApplicationVersion(VERSION)

    icon_path = resource_path("assets", "icon.ico")
    if Path(icon_path).is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    family = _preferred_font()
    if family:
        font = app.font()
        font.setFamily(family)
        app.setFont(font)

    setThemeColor(ACCENT)
    theme_name = str(settings.get("theme", "auto"))
    setTheme(
        {"light": Theme.LIGHT, "dark": Theme.DARK}.get(theme_name, Theme.AUTO),
        save=False,
    )

    _install_excepthook()

    # 界面语言必须在建窗口之前装载，否则导航与页标题会先用简中渲染一遍。
    i18n.load(str(settings.get("i18n_lang", i18n.DEFAULT_LANG)))

    # 托盘常驻：关掉最后一个窗口不等于退出进程，退出走托盘菜单的「退出」。
    QApplication.setQuitOnLastWindowClosed(False)

    # 单实例：第二次双击 exe 只把已有窗口叫到前面，不重复占端口和热键。
    # 开机自启与手动启动混用时尤其重要——否则会出现两个托盘图标、两套热键。
    window: MainWindow | None = None

    def _on_second_launch(_message: str) -> None:
        if window is not None:
            window.show_from_tray()

    guard = SingleInstance(INSTANCE_KEY)
    if bool(settings.get("single_instance", True)) and not guard.acquire(_on_second_launch):
        guard.notify("SHOW")
        return 0

    window = MainWindow()
    silent = "--silent" in sys.argv or "--minimized" in sys.argv
    window.start_background_services()

    if "--selftest" in sys.argv:
        window.show()
        code = _selftest(app, window)
        window.close()
        _final_exit(window, app, code)
        return code  # 不会执行到这里，_final_exit 直接退出进程

    if not silent:
        window.show()
    code = app.exec()
    guard.close()
    _final_exit(window, app, code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
