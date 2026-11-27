"""Application entry point."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

from PyQt6.QtGui import QFontDatabase, QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox
from qfluentwidgets import Theme, setTheme, setThemeColor

from .core import settings
from .core.config import APP_NAME, APP_SLUG, VERSION, data_dir, resource_path
from .ui.main_window import MainWindow

ACCENT = "#0078D4"


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

    report = {
        "version": VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "data_dir": str(data_dir()),
        "status": page.status_label.text(),
        "elapsed": page.elapsed_label.text(),
        "result": result,
        "image_result": image_result,
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

    # 托盘常驻：关掉最后一个窗口不等于退出进程，退出走托盘菜单的「退出」。
    QApplication.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    silent = "--silent" in sys.argv
    window.start_background_services()

    if "--selftest" in sys.argv:
        window.show()
        code = _selftest(app, window)
        window.close()
        return code

    if not silent:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
