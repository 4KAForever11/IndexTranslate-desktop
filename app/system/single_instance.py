"""单实例守护：双击 exe 只唤醒已经开着的那个，不许多开。

多开的坏处很具体：两个进程会抢同一个本地代理端口（第二个必然失败）、重复注册
全局热键（第二个也会失败），用户看到的是「报错 + 两个托盘图标」。

实现用 Qt 自带的 ``QLocalServer`` / ``QLocalSocket``（Windows 下是命名管道），
不引入任何额外依赖。第一个进程启动时监听，后续进程连上去发一条消息就退出。
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

CONNECT_TIMEOUT_MS = 400


class SingleInstance(QObject):
    """命名管道方式的单实例锁。

    典型用法::

        guard = SingleInstance("IndexTranslateDesktopInstance")
        if not guard.acquire():
            guard.notify("SHOW")   # 唤醒旧窗口
            sys.exit(0)
        guard.messageReceived.connect(on_message)
    """

    messageReceived = pyqtSignal(str)

    def __init__(self, key: str, *, timeout_ms: int = CONNECT_TIMEOUT_MS, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._key = str(key)
        self._timeout_ms = int(timeout_ms)
        self._server: QLocalServer | None = None
        self._connections: list[QLocalSocket] = []

    # ------------------------------------------------------------------ state
    @property
    def key(self) -> str:
        return self._key

    @property
    def listening(self) -> bool:
        return self._server is not None and self._server.isListening()

    # ------------------------------------------------------------------ acquire
    def _connect(self) -> QLocalSocket | None:
        socket = QLocalSocket(self)
        socket.connectToServer(self._key)
        if socket.waitForConnected(self._timeout_ms):
            return socket
        socket.abort()
        socket.deleteLater()
        return None

    def acquire(self, handler: Callable[[str], None] | None = None) -> bool:
        """尝试成为唯一实例。

        返回 ``True`` 表示已经拿到锁（并在管道上监听）；``False`` 表示已有实例
        在运行——此时应先 :meth:`notify` 唤醒它，然后退出本进程。
        """
        probe = self._connect()
        if probe is not None:
            probe.abort()
            probe.deleteLater()
            return False

        # 上次异常退出可能留下僵尸端口，先摘掉再监听。
        QLocalServer.removeServer(self._key)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self._key):
            return False
        server.newConnection.connect(self._drain)
        self._server = server
        if handler is not None:
            self.messageReceived.connect(handler)
        return True

    def _drain(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._forget(s))
            self._connections.append(socket)
            # 客户端可能已经写完并断开，先把现有数据读掉。
            if socket.bytesAvailable() > 0:
                self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        try:
            payload = bytes(socket.readAll()).decode("utf-8", "replace").strip()
        except Exception:
            payload = ""
        if payload:
            for line in payload.splitlines():
                text = line.strip()
                if text:
                    self.messageReceived.emit(text)

    def _forget(self, socket: QLocalSocket) -> None:
        if socket in self._connections:
            self._connections.remove(socket)
        socket.deleteLater()

    # ------------------------------------------------------------------ notify
    def notify(self, message: str = "SHOW") -> bool:
        """向已存在的实例发一条消息；成功返回 True。"""
        socket = self._connect()
        if socket is None:
            return False
        try:
            socket.write((str(message) + "\n").encode("utf-8"))
            socket.flush()
            socket.waitForBytesWritten(self._timeout_ms)
            socket.disconnectFromServer()
            if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                socket.waitForDisconnected(self._timeout_ms)
            return True
        except Exception:
            return False
        finally:
            socket.deleteLater()

    # ------------------------------------------------------------------ cleanup
    def close(self) -> None:
        for socket in list(self._connections):
            try:
                socket.abort()
            except Exception:
                pass
            socket.deleteLater()
        self._connections.clear()
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server.deleteLater()
            self._server = None
        try:
            QLocalServer.removeServer(self._key)
        except Exception:
            pass
