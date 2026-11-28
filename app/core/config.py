"""Application-wide constants, paths and persistent user settings.

Everything is stored locally; the app never sends anything except the
translation request itself to the public Index-Translate endpoint.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

APP_NAME = "IndexTranslate desktop"
APP_PROJECT = "IndexTranslate-desktop"
APP_SLUG = "IndexTranslate"
VERSION = "2.0.0"

API_BASE = "https://index-translate.bilibili.com/v1"
SERVICE_HOME = "https://index-translate.bilibili.com/site/home.html"
DEFAULT_MODEL = "Index-Translate-35B-A3B"

# (model id, short label shown in the UI)
MODELS: list[tuple[str, str]] = [
    ("Index-Translate-35B-A3B", "35B · 旗舰"),
    ("Index-Translate-9B", "9B · 均衡"),
    ("Index-Translate-2B", "2B · 极速"),
]
MODEL_IDS = [m for m, _ in MODELS]

# Free public endpoint: no auth, no SLA. Keep requests modest.
MAX_INPUT_CHARS = 12000
IMAGE_MAX_SIDE = 2048
IMAGE_MAX_BYTES = 4 * 1024 * 1024


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    """Path to a bundled resource, works both from source and from a frozen exe."""
    base = Path(getattr(sys, "_MEIPASS", _project_root()))
    return base.joinpath(*parts)


def model_label(code: str) -> str:
    """Short human label for a model id, e.g. Index-Translate-35B-A3B -> 35B."""
    for value, label in MODELS:
        if value == code:
            return label.split(" · ")[0]
    return (code or "").replace("Index-Translate-", "")


def data_dir() -> Path:
    """Per-user writable directory (settings, history, logs).

    ``INDEX_TRANSLATE_HOME`` overrides the location, which enables a portable
    copy and lets the packaged build be smoke-tested without touching the real
    user profile. If the preferred location is not writable (locked-down
    profiles, redirected AppData) we fall back to the system temp directory
    instead of crashing on start-up.
    """
    override = os.environ.get("INDEX_TRANSLATE_HOME")
    if override:
        candidates = [Path(override).expanduser()]
    else:
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = Path(root) if root else Path.home() / ".local" / "share"
        candidates = [base / APP_SLUG, Path(tempfile.gettempdir()) / APP_SLUG]

    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return path
        except OSError:
            continue
    return Path(tempfile.gettempdir()) / APP_SLUG


DEFAULTS: dict[str, Any] = {
    "api_base": API_BASE,
    "model": DEFAULT_MODEL,
    "source_lang": "auto",
    "target_lang": "zh",
    "stream": True,
    "theme": "auto",          # auto | light | dark
    "keep_history": 500,      # 0 = do not save history
    "temperature": 0.0,
    "instruction": "",
    "use_glossary": True,
    "timeout": 120,
    "proxy_port": 8080,
    # 代理优化：限流 / 缓存 / 术语注入 / 模型降级
    "proxy_max_concurrency": 4,
    "proxy_qps": 3.0,            # 0 = 不限速
    "proxy_cache": True,
    "proxy_cache_size": 2000,
    "proxy_inject_glossary": True,
    "proxy_fallback": True,
    "fallback_api_base": "",     # 自定义备用端点（Ollama / 硅基流动 …）
    "fallback_model": "",
    "fallback_api_key": "",
    # V2 底座：统一管道的限流 / 缓存 / 模型降级（主界面、浮窗、批量共用）
    "fallback_enabled": True,
    "fallback_models": ["Index-Translate-9B", "Index-Translate-2B"],
    "cache_enabled": True,
    "cache_ttl_days": 7,
    "cache_max_rows": 10000,
    "rate_max_concurrency": 3,
    "rate_min_interval": 0.25,   # 两次请求之间的最小间隔（秒），0 = 不限
    "advanced_open": False,
    "minimize_to_tray": True,   # 关闭窗口时收进托盘而不是退出
    "autostart": False,         # 开机静默启动（写 HKCU Run 项）
    "autostart_proxy": True,    # 静默启动时自动拉起本地代理
    "tray_tip_shown": False,    # 只提示一次「程序还在托盘里运行」
    "popup_enabled": True,      # 全局划词浮窗
    "popup_hotkey": "Ctrl+Alt+T",
    "popup_pinned": False,      # 浮窗图钉：锁定后点击外部不关闭
    "shot_hotkey": "Ctrl+Alt+S",  # 框选截屏翻译
    "capture_target": "main",   # 截屏结果给谁看：main（主窗口）| popup（浮窗）
    "replace_enabled": True,    # 输入框原地替换
    "replace_hotkey": "Ctrl+Alt+R",
    "replace_model": "Index-Translate-2B",  # 原地替换优先用极速模型
    "replace_select_all": True,  # 没有选区时先 Ctrl+A 全选
    "selection_blacklist": [],   # 这些前台进程里不模拟 Ctrl+C
    "single_instance": True,     # 只允许开一个实例
    "silent_autostart": True,    # 开机自启时静默进托盘（--minimized / --silent）
    "always_on_top": False,     # 窗口置顶
    "clipboard_watch": False,   # 剪贴板实时监听
    # 风格预设（空列表 = 使用 app/core/styles.py 里的内置预设）
    "styles": [],
    "active_style": "plain",
    # 术语表：当前分组（分组本身与启用状态存在 glossary.db 里）
    "glossary_group": "默认",
    # 输入草稿：防抖保存，重启后恢复
    "draft": {"text": "", "source_lang": "", "target_lang": "", "model": ""},
    "draft_enabled": True,
    # 批量翻译
    "batch_concurrency": 1,
    "batch_export": "bilingual",   # bilingual | target | both
    "batch_last_dir": "",
    # 扩展（Sidecar 重型能力，按需下载，不进主安装包）
    "extension_idle_sec": 300,     # 空闲多久回收扩展进程（秒）
    "extension_catalog": "",       # 扩展目录 URL（留空 = 只用本地 zip 安装）
    "ocr_overlay_enabled": True,   # 截屏翻译优先走本地 OCR 贴片
    "ocr_overlay_font_scale": 1.0,  # 贴片字号缩放
    "live_asr_model": "",          # sherpa-onnx 模型目录
    "live_subtitle_enabled": False,
    "live_subtitle_geometry": "",  # 悬浮字幕条位置
    # 多端同步（WebDAV）
    "sync_enabled": False,
    "sync_url": "",
    "sync_user": "",
    "sync_password": "",           # DPAPI 加密后的密文，见 app/core/sync/webdav_sync.py
    "sync_auto": False,
    "sync_last_at": 0.0,
    "sync_last_result": "",
    # 自动更新
    "update_channel": "stable",
    "update_checked_at": 0.0,
    "update_skip_version": "",
    # 外观 / 语言
    "mica": True,                  # Win11 会被系统忽略，Win10 自动回退纯色
    "i18n_lang": "zh_CN",          # zh_CN | zh_TW | en_US
    "mini_mode": False,            # 迷你悬浮条形态
    "window_geometry": "",
    "window_state": "",
}


class Settings:
    """Tiny JSON-backed settings store (human readable, crash safe)."""

    def __init__(self) -> None:
        self.path = data_dir() / "settings.json"
        self._data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in DEFAULTS:
                        self._data[key] = value
        except FileNotFoundError:
            pass
        except Exception:
            # A corrupt settings file must never prevent the app from starting.
            pass

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if key in DEFAULTS:
                self._data[key] = value

    def values(self) -> dict[str, Any]:
        """A shallow copy of every stored value (used by the sync snapshot)."""
        return dict(self._data)


settings = Settings()
