"""Sidecar 扩展客户端：子进程 + JSONL 管道 + Win32 Job Object。

设计要点（对应 doc/DEVELOPMENT-V2-Final.md 模块 1）：

* **不进主进程**：重型依赖（PyMuPDF / ONNX Runtime / sherpa-onnx）永远活在这个子进程里，
  崩了也只是这一行任务失败，托盘、代理、主窗口都不受影响。
* **不开端口**：走 stdin/stdout 的 JSON Lines，不会弹 Windows 防火墙。
* **不留僵尸**：子进程被绑到带 ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` 的 Job Object，
  主进程被强杀时 Windows 内核会顺手收掉它。
* **不吃内存**：空闲超过 ``idle_timeout_sec`` 自动优雅退出，下次用到再冷启动。

协议（每行一个 JSON）::

    请求   {"id": 1, "method": "ocr", "params": {"image": "..."}}
    进度   {"id": 1, "event": "chunk", "data": {"text": "..."}}
    成功   {"id": 1, "ok": true, "result": {...}}
    失败   {"id": 1, "ok": false, "error": "缺少依赖 ..."}

子进程的日志一律写 stderr，这里只保留最后若干行用于报错。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterator

CREATE_NO_WINDOW = 0x08000000
DEFAULT_TIMEOUT = 120.0
IDLE_TIMEOUT = 300.0
_STDERR_LINES = 60


class SidecarError(RuntimeError):
    """子进程调用失败（消息可直接展示给用户）。"""


class SidecarTimeout(SidecarError):
    """子进程超时。"""


class _Dead:
    """reader 线程发现管道断开时投递的哨兵。"""


def _job_object():
    """创建 kill-on-close 的 Job Object；拿不到就返回 None（退化为普通子进程）。"""
    try:
        import win32job
    except Exception:  # pragma: no cover - 非 Windows / 缺 pywin32
        return None
    try:
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        return job
    except Exception:  # pragma: no cover - 老系统或权限受限
        return None


class SidecarClient:
    """一个扩展对应一个客户端；进程按需拉起、空闲回收。"""

    def __init__(
        self,
        entry: str | Path,
        *,
        name: str = "",
        idle_timeout_sec: float = IDLE_TIMEOUT,
        python: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.entry = Path(entry)
        self.name = name or self.entry.stem
        self.idle_timeout_sec = float(idle_timeout_sec)
        self._python = python
        self._env_extra = dict(env or {})

        self._proc: subprocess.Popen[str] | None = None
        self._job = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._queues: dict[int, queue.Queue[Any]] = {}
        self._next_id = 0
        self._reader: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=_STDERR_LINES)
        self._subscribers: list[Any] = []
        self._sub_lock = threading.Lock()
        self._last_used = 0.0
        self._starting = False
        self._restarts = 0

    # ------------------------------------------------------------- 进程管理
    @property
    def running(self) -> bool:
        proc = self._proc
        return bool(proc and proc.poll() is None)

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc and proc.poll() is None else None

    @property
    def restarts(self) -> int:
        return self._restarts

    def _command(self) -> list[str]:
        if self.entry.suffix.lower() == ".py":
            python = self._python or sys.executable
            # -u：不吃缓冲，否则流式事件会被卡在子进程里
            return [python, "-u", str(self.entry)]
        return [str(self.entry)]

    def _cwd(self) -> str:
        return str(self.entry.parent if self.entry.parent.is_dir() else Path.cwd())

    def ensure_started(self) -> None:
        with self._lock:
            if self.running:
                return
            if self._proc is not None:
                # 上一轮的子进程已经死了：先收干净句柄再重开
                self.stop()
            if not self.entry.exists():
                raise SidecarError(f"扩展程序不存在：{self.entry.name}")
            if self._starting:
                # 另一个线程正在启动，等它拉起
                for _ in range(100):
                    if self.running:
                        return
                    time.sleep(0.02)
                raise SidecarError(f"扩展「{self.name}」启动超时。")

            self._starting = True
            try:
                env = os.environ.copy()
                env.setdefault("PYTHONIOENCODING", "utf-8")
                env.update(self._env_extra)
                flags = CREATE_NO_WINDOW if os.name == "nt" else 0
                try:
                    self._proc = subprocess.Popen(  # noqa: S603 - 路径来自已安装扩展
                        self._command(),
                        cwd=self._cwd(),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        creationflags=flags,
                        env=env,
                    )
                except OSError as exc:
                    raise SidecarError(f"无法启动扩展「{self.name}」：{exc}") from exc

                self._job = _job_object()
                if self._job is not None:
                    try:
                        import win32job

                        win32job.AssignProcessToJobObject(
                            self._job, int(self._proc._handle)  # noqa: SLF001
                        )
                    except Exception:
                        pass

                self._stderr_lines.clear()
                self._reader = threading.Thread(
                    target=self._read_stdout, name=f"sidecar-{self.name}", daemon=True
                )
                self._reader.start()
                threading.Thread(
                    target=self._read_stderr, name=f"sidecar-{self.name}-err", daemon=True
                ).start()
                self._last_used = time.monotonic()
            finally:
                self._starting = False

    def _read_stdout(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._stderr_lines.append(f"[非 JSON 输出] {line[:200]}")
                continue
            if not isinstance(message, dict):
                continue
            req_id = message.get("id")
            if message.get("event"):
                # 主动推送（实时字幕这类长任务）：先给订阅者，再交给等待中的请求
                self._notify(message)
            if isinstance(req_id, int):
                target = self._queues.get(req_id)
                if target is not None:
                    target.put(message)
        # 管道关闭：唤醒所有等待者
        for target in list(self._queues.values()):
            target.put(_Dead())

    def _read_stderr(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                self._stderr_lines.append(line)
                if os.environ.get("INDEX_TRANSLATE_SIDECAR_DEBUG"):
                    print(f"[{self.name}] {line}", file=sys.stderr)

    def stderr_tail(self, lines: int = 8) -> str:
        return "\n".join(list(self._stderr_lines)[-lines:])

    # ------------------------------------------------------------- 事件订阅
    def add_subscriber(self, handler: Any) -> None:
        """订阅子进程主动推送的事件（``{"id":..,"event":..,"data":..}``）。"""
        with self._sub_lock:
            if handler not in self._subscribers:
                self._subscribers.append(handler)

    def remove_subscriber(self, handler: Any) -> None:
        with self._sub_lock:
            if handler in self._subscribers:
                self._subscribers.remove(handler)

    def _notify(self, message: dict[str, Any]) -> None:
        with self._sub_lock:
            handlers = list(self._subscribers)
        for handler in handlers:
            try:
                handler(message)
            except Exception:  # 订阅者出错不能拖垮 reader 线程
                continue

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                except Exception:
                    pass
            if proc is not None:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
            self._job = None
            for target in list(self._queues.values()):
                target.put(_Dead())
            self._queues.clear()

    def reclaim_if_idle(self, now: float | None = None) -> bool:
        """空闲超时就回收；返回是否真的回收了。"""
        moment = time.monotonic() if now is None else now
        with self._lock:
            if not self.running:
                return False
            if (moment - self._last_used) <= self.idle_timeout_sec:
                return False
        self.stop()
        return True

    # ------------------------------------------------------------- 请求
    def _send(self, req_id: int, method: str, params: dict[str, Any] | None) -> None:
        proc = self._proc
        if not proc or not proc.stdin:
            raise SidecarError(f"扩展「{self.name}」未启动。")
        payload = json.dumps(
            {"id": req_id, "method": method, "params": params or {}}, ensure_ascii=False
        )
        with self._write_lock:
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise SidecarError(f"扩展「{self.name}」连接已断开：{exc}") from exc

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """同步调用，返回 ``result`` 字段。"""
        with self._lock:
            self.ensure_started()
            self._next_id += 1
            req_id = self._next_id
            holder: queue.Queue[Any] = queue.Queue()
            self._queues[req_id] = holder

        try:
            self._send(req_id, method, params)

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SidecarTimeout(f"扩展「{self.name}」响应超时（{timeout:.0f} 秒）。")
                try:
                    message = holder.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    continue
                if isinstance(message, _Dead):
                    detail = self.stderr_tail()
                    raise SidecarError(
                        f"扩展「{self.name}」意外退出。" + (f"\n{detail}" if detail else "")
                    )
                if message.get("event"):
                    continue  # 同步调用忽略流式事件
                self._last_used = time.monotonic()
                if message.get("ok"):
                    result = message.get("result")
                    return result if isinstance(result, dict) else {"value": result}
                raise SidecarError(
                    str(message.get("error") or f"扩展「{self.name}」处理失败。")
                )
        finally:
            with self._lock:
                self._queues.pop(req_id, None)

    def stream(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Iterator[dict[str, Any]]:
        """流式调用：逐条 yield ``{"event": ..., "data": {...}}``，最后 yield
        ``{"event": "result", "data": <result>}``；失败抛 SidecarError。"""
        with self._lock:
            self.ensure_started()
            self._next_id += 1
            req_id = self._next_id
            holder: queue.Queue[Any] = queue.Queue()
            self._queues[req_id] = holder

        try:
            self._send(req_id, method, params)

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SidecarTimeout(f"扩展「{self.name}」响应超时（{timeout:.0f} 秒）。")
                try:
                    message = holder.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    continue
                if isinstance(message, _Dead):
                    detail = self.stderr_tail()
                    raise SidecarError(
                        f"扩展「{self.name}」意外退出。" + (f"\n{detail}" if detail else "")
                    )
                self._last_used = time.monotonic()
                if message.get("event"):
                    yield {
                        "event": str(message.get("event")),
                        "data": message.get("data") or {},
                    }
                    continue
                if message.get("ok"):
                    result = message.get("result")
                    yield {
                        "event": "result",
                        "data": result if isinstance(result, dict) else {"value": result},
                    }
                    return
                raise SidecarError(
                    str(message.get("error") or f"扩展「{self.name}」处理失败。")
                )
        finally:
            with self._lock:
                self._queues.pop(req_id, None)

    def ping(self, timeout: float = 20.0) -> dict[str, Any]:
        return self.call("ping", {}, timeout=timeout)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        state = "running" if self.running else "idle"
        return f"<SidecarClient {self.name} {state} pid={self.pid}>"


def which_python() -> str:
    """打包后没有 python.exe；扩展若带 .py 入口则退化为当前解释器。"""
    return sys.executable or shutil.which("python") or "python"
