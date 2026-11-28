"""扩展中心：安装 / 启停 / 测试 / 卸载重型 Sidecar 扩展。

主程序永远是纯 API 轻核心：PDF、OCR、语音识别都跑在独立子进程里，
这里只负责文件、状态与进程生命周期。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    SpinBox,
    SwitchButton,
    TitleLabel,
)

from ..core import settings
from ..core.extensions.catalog import (
    CatalogEntry,
    CatalogError,
    catalog_url,
    fetch_catalog,
)
from ..core.extensions.manager import ExtensionError, download_extension
from ..core.extensions.runtime import DEFAULT_IDLE_SEC, ExtensionRuntime
from ..core.extensions.sidecar_client import SidecarError
from .theme import Card, MutedLabel, PageWidget, StatusDot, TextLabel, colors

SUGGESTED = (
    ("ext-ocr-overlay", "本地 OCR 原位贴片", "RapidOCR · 30–45 MB", "截图 → 本地识别 → 原位译文贴片"),
    ("ext-pdf-bilingual", "PDF 双语对照", "PyMuPDF · 35–50 MB", "版面提取 → 队列翻译 → 左右对照导出"),
    ("ext-live-asr", "系统音频实时字幕", "PyAudioWPatch + sherpa-onnx · 160–260 MB", "回环抓音 → VAD 断句 → 置顶字幕"),
)


def _size_text(num: int) -> str:
    if num <= 0:
        return "—"
    if num < 1024 * 1024:
        return f"{num / 1024:.0f} KB"
    return f"{num / 1024 / 1024:.1f} MB"


class CatalogWorker(QThread):
    """后台拉目录：raw.githubusercontent 偶尔很慢，不能卡住 UI 线程。"""

    loaded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, url: str, parent=None) -> None:
        super().__init__(parent)
        self._url = url

    def run(self) -> None:  # noqa: D401 - QThread 入口
        try:
            entries = fetch_catalog(self._url)
        except CatalogError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成界面文字
            self.failed.emit(f"读取扩展目录失败：{exc}")
            return
        self.loaded.emit(entries)


class CatalogInstallWorker(QThread):
    """下载 + 校验 + 安装，全程在后台，失败原因原样抛回界面。"""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, entry: CatalogEntry, manager, host_version: str, parent=None) -> None:
        super().__init__(parent)
        self._entry = entry
        self._manager = manager
        self._host_version = host_version

    def run(self) -> None:  # noqa: D401 - QThread 入口
        entry = self._entry
        tmp_dir = Path(tempfile.mkdtemp(prefix="it-ext-"))
        target = tmp_dir / f"{entry.id}-{entry.version}.zip"
        try:
            download_extension(entry.url, target)
            installed = self._manager.install_zip(
                target,
                expected_sha256=entry.sha256 or None,
                source=entry.url,
                host_version=self._host_version,
            )
        except (CatalogError, ExtensionError, ValueError) as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"安装 {entry.id} 失败：{exc}")
            return
        finally:
            try:
                target.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except OSError:
                pass
        self.done.emit(installed)


class ExtensionCard(Card):
    """一个已安装扩展的卡片。"""

    def __init__(self, row: dict, runtime: ExtensionRuntime, page: "ExtensionsPage", parent=None) -> None:
        super().__init__(parent, radius=10)
        self._row = row
        self._runtime = runtime
        self._page = page
        self.setMinimumHeight(112)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = TextLabel(str(row["name"]), size=14, bold=True)
        header.addWidget(title)
        header.addWidget(MutedLabel(f"v{row['version']}", size=12))
        header.addWidget(MutedLabel(f"· {_size_text(row['size'])}", size=12))
        header.addStretch(1)
        self._dot = StatusDot()
        self._dot.setFixedSize(8, 8)
        header.addWidget(self._dot)
        self._state = MutedLabel("", size=12)
        header.addWidget(self._state)
        layout.addLayout(header)

        if row["description"]:
            desc = MutedLabel(str(row["description"]), size=12)
            desc.setWordWrap(True)
            layout.addWidget(desc)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self._switch = SwitchButton()
        self._switch.setOffText("已停用")
        self._switch.setOnText("已启用")
        self._switch.setChecked(bool(row["enabled"]))
        self._switch.checkedChanged.connect(self._on_toggled)
        actions.addWidget(self._switch)
        actions.addStretch(1)

        self._detail = MutedLabel("", size=11)
        self._detail.setMinimumWidth(260)
        self._detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        actions.addWidget(self._detail)

        self._test_button = PushButton("测试连接")
        self._test_button.clicked.connect(self._on_test)
        actions.addWidget(self._test_button)

        self._remove_button = PushButton("卸载")
        self._remove_button.clicked.connect(self._on_uninstall)
        actions.addWidget(self._remove_button)
        layout.addLayout(actions)

        self.refresh_state()

    # ------------------------------------------------------------------ 状态
    def update_row(self, row: dict) -> None:
        self._row = row
        self._switch.blockSignals(True)
        self._switch.setChecked(bool(row["enabled"]))
        self._switch.blockSignals(False)
        self.refresh_state()

    def refresh_state(self) -> None:
        row = self._row
        color = colors()
        if not row["host_ok"]:
            self._dot.set_color(color["warn"])
            self._state.setText("需要新版宿主")
            self._detail.setText(str(row["host_error"]))
            self._switch.setEnabled(False)
            self._test_button.setEnabled(False)
            return
        self._switch.setEnabled(True)
        if not row["enabled"]:
            self._dot.set_color(color["faint"])
            self._state.setText("已停用")
            self._detail.setText("停用后不会启动子进程")
            self._test_button.setEnabled(False)
            return
        self._test_button.setEnabled(True)
        if row["running"]:
            self._dot.set_color(color["ok"])
            self._state.setText("运行中")
            restarts = int(row["restarts"] or 0)
            text = f"PID {row['pid']}"
            if restarts:
                text += f" · 重启 {restarts} 次"
            self._detail.setText(text)
        else:
            self._dot.set_color(color["faint"])
            self._state.setText("未启动")
            self._detail.setText("首次使用时会自动拉起子进程")

    # ------------------------------------------------------------------ 交互
    def _on_toggled(self, checked: bool) -> None:
        self._runtime.set_enabled(str(self._row["id"]), bool(checked))
        self._page.refresh()

    def _on_test(self) -> None:
        self._test_button.setEnabled(False)
        self._detail.setText("正在测试…")
        ok, message = self._runtime.ping(str(self._row["id"]))
        self._test_button.setEnabled(True)
        if ok:
            self._detail.setText(message)
            InfoBar.success(
                "扩展可用",
                f"{self._row['name']}：{message}",
                duration=2600,
                position=InfoBarPosition.BOTTOM_RIGHT,
                parent=self._page.window(),
            )
        else:
            self._detail.setText(message.splitlines()[0][:80])
            InfoBar.error(
                "扩展不可用",
                message[:300],
                duration=6000,
                position=InfoBarPosition.BOTTOM_RIGHT,
                parent=self._page.window(),
            )
        self._page.refresh(keep_cards=True)

    def _on_uninstall(self) -> None:
        box = MessageBox(
            "卸载扩展",
            f"确定卸载「{self._row['name']}」？扩展包会被删除，重新安装需要再次下载。",
            self._page.window(),
        )
        if not box.exec():
            return
        self._runtime.uninstall(str(self._row["id"]))
        self._page.refresh()


class ExtensionsPage(PageWidget):
    """扩展中心页。"""

    def __init__(self, runtime: ExtensionRuntime, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._cards: dict[str, ExtensionCard] = {}
        self.runtime_changed = runtime.changed
        self._catalog: list[CatalogEntry] = []
        self._catalog_state = "尚未刷新"
        self._catalog_error = ""
        self._catalog_worker: CatalogWorker | None = None
        self._install_worker: CatalogInstallWorker | None = None
        self._catalog_auto = False

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(TitleLabel("扩展中心"))
        header.addStretch(1)
        self._install_button = PrimaryPushButton(FluentIcon.ADD, "安装扩展")
        self._install_button.clicked.connect(self._install)
        header.addWidget(self._install_button)
        self._folder_button = PushButton(FluentIcon.FOLDER, "扩展目录")
        self._folder_button.clicked.connect(self._open_folder)
        header.addWidget(self._folder_button)
        root.addLayout(header)

        subtitle = MutedLabel(
            "PDF 双语对照、本地 OCR 原位贴片、系统音频实时字幕都跑在独立子进程里："
            "崩了只影响那一条任务，主程序始终保持在 60 MB 以内。",
            size=12,
        )
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        tools = Card(radius=8)
        tools_layout = QHBoxLayout(tools)
        tools_layout.setContentsMargins(20, 12, 20, 12)
        tools_layout.setSpacing(12)
        tools_layout.addWidget(TextLabel("空闲回收", size=13, bold=True))
        self._idle_spin = SpinBox()
        self._idle_spin.setRange(60, 1800)
        self._idle_spin.setSingleStep(30)
        self._idle_spin.setSuffix(" 秒")
        self._idle_spin.setFixedWidth(120)
        self._idle_spin.setValue(int(float(settings.get("extension_idle_sec", DEFAULT_IDLE_SEC))))
        self._idle_spin.valueChanged.connect(self._on_idle_changed)
        tools_layout.addWidget(self._idle_spin)
        tools_layout.addWidget(
            MutedLabel("超过这个时间没被调用，扩展进程会自动退出，把内存还给系统。", size=11)
        )
        tools_layout.addStretch(1)
        self._reclaim_label = MutedLabel("", size=11)
        tools_layout.addWidget(self._reclaim_label)
        root.addWidget(tools)

        self._scroll = SmoothScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(SmoothScrollArea.Shape.NoFrame)
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        self._list = QVBoxLayout(host)
        self._list.setContentsMargins(0, 2, 6, 8)
        self._list.setSpacing(10)
        self._scroll.setWidget(host)
        root.addWidget(self._scroll, 1)

        self.refresh()
        QTimer.singleShot(0, self._maybe_autoload_catalog)

    # ------------------------------------------------------------------ 列表
    def refresh(self, *, keep_cards: bool = False) -> None:
        rows = self._runtime.status()
        ids = [row["id"] for row in rows]

        if keep_cards and ids == list(self._cards):
            for row in rows:
                card = self._cards.get(row["id"])
                if card is not None:
                    card.update_row(row)
            self._update_summary(rows)
            return

        for card in self._cards.values():
            self._list.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._list.addWidget(self._catalog_block())
        if not rows:
            self._list.addWidget(self._empty_block())

        for row in rows:
            card = ExtensionCard(row, self._runtime, self)
            self._cards[row["id"]] = card
            self._list.addWidget(card)

        self._list.addWidget(self._suggest_block(rows))
        self._list.addStretch(1)
        self._update_summary(rows)

    def _update_summary(self, rows: list[dict]) -> None:
        running = [row for row in rows if row["running"]]
        if not rows:
            self._reclaim_label.setText("尚未安装任何扩展")
        else:
            self._reclaim_label.setText(
                f"已安装 {len(rows)} 个 · 运行中 {len(running)} 个"
            )

    def _empty_block(self) -> QWidget:
        card = Card(radius=10)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)
        layout.addWidget(TextLabel("还没有安装扩展", size=13, bold=True))
        hint = MutedLabel(
            "点右上角「安装扩展」选择扩展 zip 即可。扩展包会解压到数据目录的 extensions/ 下，"
            "卸载时整目录删除，不会在主程序里留下任何重型依赖。",
            size=12,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return card

    def _suggest_block(self, rows: list[dict]) -> QWidget:
        installed = {row["id"] for row in rows}
        card = Card(radius=10)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(TextLabel("可用的重型扩展", size=13, bold=True))
        hint = MutedLabel(
            "源码在仓库 extensions_src/ 下，每个扩展单独用 PyInstaller 打包成 zip 发布；"
            "主程序只认 manifest.json 里的 id / version / sha256。",
            size=11,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        for ext_id, name, size, flow in SUGGESTED:
            row = QHBoxLayout()
            row.setSpacing(8)
            mark = "已安装" if ext_id in installed else "未安装"
            row.addWidget(TextLabel(f"{name}", size=12, bold=True))
            row.addWidget(MutedLabel(f"{ext_id} · {size} · {flow} · {mark}", size=11))
            row.addStretch(1)
            layout.addLayout(row)
        return card

    # ------------------------------------------------------------------ 操作
    def _catalog_block(self) -> QWidget:
        """在线目录：官方仓库里的静态清单，一键装 / 更新。"""
        card = Card(radius=10)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(TextLabel("在线目录", size=13, bold=True))
        head.addWidget(MutedLabel(self._catalog_state, size=11))
        head.addStretch(1)
        refresh_button = PushButton(FluentIcon.SYNC, "刷新目录")
        refresh_button.setEnabled(self._catalog_worker is None)
        refresh_button.clicked.connect(self.load_catalog)
        head.addWidget(refresh_button)
        url_button = PushButton("换地址")
        url_button.setToolTip("GitHub 打不开时可以换成自己的镜像地址")
        url_button.clicked.connect(self._edit_catalog_url)
        head.addWidget(url_button)
        layout.addLayout(head)

        url_text = catalog_url()
        url_label = MutedLabel(url_text or "未配置目录地址", size=10)
        url_label.setWordWrap(True)
        layout.addWidget(url_label)

        if self._catalog_error:
            err = MutedLabel(self._catalog_error, size=11)
            err.setWordWrap(True)
            layout.addWidget(err)

        installed = {row["id"]: row for row in self._runtime.status()}
        for entry in self._catalog:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(TextLabel(entry.name, size=12, bold=True))
            row.addWidget(MutedLabel(f"v{entry.version} · {entry.size_text}", size=11))
            have = installed.get(entry.id)
            if not entry.host_ok(self._runtime.host_version):
                row.addWidget(MutedLabel(f"需要宿主 {entry.host_min}+", size=11))
                button_text = ""
            elif have is None:
                button_text = "安装"
            elif have.get("version") != entry.version:
                button_text = f"更新到 v{entry.version}"
            else:
                button_text = "已是最新"
            row.addStretch(1)
            if button_text:
                button = PushButton(button_text)
                button.setMinimumWidth(120)
                can_install = self._install_worker is None and not (
                    have is not None and have.get("version") == entry.version
                )
                button.setEnabled(can_install)
                button.clicked.connect(
                    lambda _checked=False, item=entry: self._install_from_catalog(item)
                )
                row.addWidget(button)
            layout.addLayout(row)
            if entry.description:
                desc = MutedLabel(entry.description, size=10)
                desc.setWordWrap(True)
                layout.addWidget(desc)

        if not self._catalog and not self._catalog_error:
            hint = MutedLabel(
                "目录还没加载。这里只认官方仓库发布的清单，以及清单里写的 SHA-256："
                "哈希对不上的包会直接拒绝安装。",
                size=11,
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
        return card

    # -------------------------------------------------------------- 在线目录
    def _edit_catalog_url(self) -> None:
        from PyQt6.QtWidgets import QInputDialog

        current = catalog_url()
        text, ok = QInputDialog.getText(
            self.window(),
            "扩展目录地址",
            "填官方清单或镜像的 JSON 地址（留空恢复默认）：",
            text=current,
        )
        if not ok:
            return
        settings.set("extension_catalog", text.strip())
        settings.save()
        self._catalog = []
        self._catalog_error = ""
        self._catalog_state = "尚未刷新"
        if catalog_url():
            self.load_catalog()
        else:
            self.refresh()

    def _maybe_autoload_catalog(self) -> None:
        if self._catalog_auto:
            return
        self._catalog_auto = True
        if not self._catalog and catalog_url():
            self.load_catalog()

    def load_catalog(self) -> None:
        if self._catalog_worker is not None:
            return
        url = catalog_url()
        if not url:
            self._catalog_error = "没有配置扩展目录地址（设置里可以填镜像）。"
            self.refresh()
            return
        self._catalog_state = "正在刷新…"
        self._catalog_error = ""
        self.refresh()
        worker = CatalogWorker(url, self)
        self._catalog_worker = worker
        worker.loaded.connect(self._on_catalog_loaded)
        worker.failed.connect(self._on_catalog_failed)
        worker.finished.connect(self._on_catalog_finished)
        worker.start()

    def _on_catalog_loaded(self, entries) -> None:
        self._catalog = list(entries)
        self._catalog_state = f"共 {len(self._catalog)} 个扩展"
        self.refresh()

    def _on_catalog_failed(self, message: str) -> None:
        self._catalog_error = message
        self._catalog_state = "刷新失败"
        self.refresh()

    def _on_catalog_finished(self) -> None:
        worker = self._catalog_worker
        self._catalog_worker = None
        if worker is not None:
            worker.deleteLater()
        self.refresh(keep_cards=True)

    def _install_from_catalog(self, entry: CatalogEntry) -> None:
        if self._install_worker is not None:
            return
        self._catalog_state = f"正在下载 {entry.name}…"
        self.refresh()
        worker = CatalogInstallWorker(
            entry, self._runtime.manager, self._runtime.host_version, self
        )
        self._install_worker = worker
        worker.done.connect(self._on_catalog_installed)
        worker.failed.connect(self._on_catalog_install_failed)
        worker.finished.connect(self._on_catalog_install_finished)
        worker.start()

    def _on_catalog_installed(self, installed) -> None:
        self._catalog_state = f"已安装 {installed.manifest.name} v{installed.manifest.version}"
        InfoBar.success(
            "扩展已安装",
            f"{installed.manifest.name} v{installed.manifest.version}",
            duration=2600,
            position=InfoBarPosition.BOTTOM_RIGHT,
            parent=self.window(),
        )
        self.refresh()

    def _on_catalog_install_failed(self, message: str) -> None:
        self._catalog_state = "安装失败"
        InfoBar.error(
            "安装失败",
            message,
            duration=7000,
            position=InfoBarPosition.BOTTOM_RIGHT,
            parent=self.window(),
        )
        self.refresh()

    def _on_catalog_install_finished(self) -> None:
        worker = self._install_worker
        self._install_worker = None
        if worker is not None:
            worker.deleteLater()
        self.refresh(keep_cards=True)

    def _install(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window(),
            "选择扩展包",
            str(Path.home()),
            "扩展包 (*.zip);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            installed = self._runtime.manager.install_zip(
                path, host_version=self._runtime.host_version
            )
        except Exception as exc:  # ManifestError / ExtensionError 都要给用户看得懂的话
            InfoBar.error(
                "安装失败",
                str(exc),
                duration=6000,
                position=InfoBarPosition.BOTTOM_RIGHT,
                parent=self.window(),
            )
            return
        self.refresh()
        InfoBar.success(
            "扩展已安装",
            f"{installed.manifest.name} v{installed.manifest.version}",
            duration=2600,
            position=InfoBarPosition.BOTTOM_RIGHT,
            parent=self.window(),
        )

    def _open_folder(self) -> None:
        folder = self._runtime.manager.root
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(folder))  # noqa: S606 - Windows 打开资源管理器
        except OSError:
            subprocess.Popen(["explorer", str(folder)])  # noqa: S607

    def _on_idle_changed(self, value: int) -> None:
        settings.set("extension_idle_sec", int(value))
        settings.save()

    def shutdown(self) -> None:
        self._runtime.shutdown()