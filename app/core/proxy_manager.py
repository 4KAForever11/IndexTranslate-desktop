"""本地 OpenAI 兼容代理的进程级管理者。

代理的生命周期不属于「设置页」——关掉窗口、最小化到托盘、开机静默启动时，
代理都必须继续工作，否则沉浸式翻译会当场断流。所以这里把它抽成一个
``QObject``，由 ``MainWindow`` 持有，设置页和托盘只负责调用与展示状态。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from .config import API_BASE, DEFAULT_MODEL, settings
from .proxy_server import ProxyServer

# 设置键 → ProxyServer 策略键
_POLICY_KEYS = {
    "proxy_max_concurrency": "max_concurrency",
    "proxy_qps": "qps",
    "proxy_cache": "cache_enabled",
    "proxy_cache_size": "cache_size",
    "proxy_inject_glossary": "inject_glossary",
    "proxy_fallback": "fallback_enabled",
    "fallback_api_base": "fallback_api_base",
    "fallback_model": "fallback_model",
    "fallback_api_key": "fallback_api_key",
}


def policy_from_settings() -> dict[str, Any]:
    return {target: settings.get(source) for source, target in _POLICY_KEYS.items()}


class ProxyManager(QObject):
    """按设置启动/停止 ``ProxyServer``，并把结果广播给所有界面。"""

    started = pyqtSignal(str)      # http://127.0.0.1:8080/v1
    stopped = pyqtSignal()
    failed = pyqtSignal(str)       # 可直接展示的中文错误

    def __init__(self, parent: QObject | None = None, glossary: Any = None) -> None:
        super().__init__(parent)
        self._server: ProxyServer | None = None
        self._glossary = glossary

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._server is not None and self._server.running

    @property
    def url(self) -> str:
        return self._server.url if self.running and self._server else ""

    @property
    def server(self) -> ProxyServer | None:
        return self._server

    def set_glossary(self, glossary: Any) -> None:
        """术语表在窗口启动后才建好，代理运行时需要补上。"""
        self._glossary = glossary
        if self._server is not None:
            self._server.set_glossary(glossary)

    def _build(self) -> ProxyServer:
        return ProxyServer(
            port=int(settings.get("proxy_port", 8080) or 8080),
            api_base=str(settings.get("api_base", API_BASE)),
            model=str(settings.get("model", DEFAULT_MODEL)),
            timeout=float(settings.get("timeout", 120) or 120),
            glossary=self._glossary,
            policy=policy_from_settings(),
        )

    # ---------------------------------------------------------------- control
    def start(self) -> bool:
        """启动（已在运行时直接返回 True）。"""
        if self.running:
            self.started.emit(self.url)
            return True

        server = self._build()
        try:
            url = server.start()
        except RuntimeError as exc:
            self._server = None
            self.failed.emit(str(exc))
            return False
        except Exception as exc:  # 端口被占用之外的意外错误
            self._server = None
            self.failed.emit(f"代理启动失败：{exc}")
            return False

        self._server = server
        self.started.emit(url)
        return True

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        try:
            server.stop()
        finally:
            self.stopped.emit()

    def toggle(self) -> bool:
        if self.running:
            self.stop()
            return False
        return self.start()

    def apply_settings(self) -> None:
        """接口地址、模型与限流/缓存策略改动后热更新（端口变更由调用方先 stop）。"""
        if not self.running or self._server is None:
            return
        self._server.update(
            api_base=str(settings.get("api_base", API_BASE)).rstrip("/"),
            model=str(settings.get("model", DEFAULT_MODEL)),
        )
        self._server.apply_policy(**policy_from_settings())

    def stats(self) -> dict[str, Any]:
        return self._server.stats() if self.running and self._server else {}

    def clear_cache(self) -> int:
        return self._server.clear_cache() if self.running and self._server else 0

    def shutdown(self) -> None:
        self.stop()
