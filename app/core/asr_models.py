"""语音识别模型（sherpa-onnx SenseVoice）的下载与状态判断。

扩展本身只带运行时代码，模型有 228 MB，必须单独拿。这里负责：

* 说清楚「现在有没有模型、放在哪、还差多少」；
* 从国内的 hf-mirror 优先、HuggingFace 兜底，断点续传落到
  ``%LOCALAPPDATA%\\IndexTranslate\\models\\sensevoice-small``；
* 校验文件大小，坏文件不会被当成可用模型。

UI 层只调用 :func:`describe` / :func:`configured_dir` / :func:`download`。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import httpx

from .config import data_dir, settings
from .http_client import new_client

MODEL_ID = "sensevoice-small"
MODEL_TITLE = "SenseVoice 多语种（中英日韩粤）"
HF_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
#: 文件名与官方字节数；文件名顺序即「优先使用的模型文件」。
MODEL_FILES: tuple[tuple[str, int], ...] = (
    ("model.int8.onnx", 239_233_841),
    ("tokens.txt", 315_894),
)
MODEL_NAMES = tuple(name for name, _ in MODEL_FILES)
PRIMARY_MODEL = MODEL_FILES[0][0]
#: 识别器自己认的文件名，按优先级排列（发布包里是 int8，自备模型可能是 fp32）。
MODEL_CANDIDATES = (PRIMARY_MODEL, "model.onnx")
MIRRORS = (
    f"https://hf-mirror.com/{HF_REPO}/resolve/main",
    f"https://huggingface.co/{HF_REPO}/resolve/main",
)
CHUNK = 262_144
READ_TIMEOUT = 180.0

ProgressFn = Callable[[int, int, str], None]


class AsrModelError(RuntimeError):
    """下载或校验失败。``detail`` 是给用户看的那一句。"""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or message


def total_bytes() -> int:
    return sum(size for _, size in MODEL_FILES)


def models_root(base: Path | None = None) -> Path:
    return (base or Path(data_dir())) / "models"


def target_dir(base: Path | None = None) -> Path:
    return models_root(base) / MODEL_ID


def model_file(path: str | Path) -> str:
    """返回目录里实际可用的模型文件（int8 优先），没有则空串。"""
    folder = Path(path)
    for name in MODEL_CANDIDATES:
        candidate = folder / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return str(candidate)
    return ""


def _size_ok(file: Path, expected: int) -> bool:
    try:
        actual = file.stat().st_size
    except OSError:
        return False
    if expected <= 0:
        return actual > 0
    return actual == expected


def is_ready(path: str | Path | None = None) -> bool:
    folder = Path(path or target_dir())
    if not folder.is_dir():
        return False
    if not model_file(folder):
        return False
    tokens = folder / "tokens.txt"
    return tokens.is_file() and tokens.stat().st_size > 0


def describe(path: str | Path | None = None) -> tuple[str, bool]:
    """(给用户看的状态文案, 是否可用)。"""
    folder = Path(path or target_dir())
    if is_ready(folder):
        size = sum(f.stat().st_size for f in folder.glob("*") if f.is_file())
        return f"已就绪（{size / 1048576:.0f} MB）· {folder}", True
    if not folder.exists():
        return f"未下载（需要 {total_bytes() / 1048576:.0f} MB）", False
    missing = [name for name in MODEL_NAMES if not (folder / name).is_file()]
    models = [name for name in MODEL_NAMES[:-1] if (folder / name).is_file()]
    if missing and models:
        return f"缺少 {', '.join(missing)}", False
    partial = [name for name in MODEL_NAMES if (folder / f"{name}.part").exists()]
    if partial:
        return f"下载未完成（{', '.join(partial)}）", False
    return "目录里没有可用的模型文件", False


def configured_dir() -> str:
    """设置里指定的目录 → 官方下载目录 → 空串。"""
    configured = str(settings.get("live_asr_model", "") or "")
    if configured and is_ready(configured):
        return configured
    default = target_dir()
    if is_ready(default):
        return str(default)
    return ""


def _download_once(url: str, dest: Path, expected: int, progress: ProgressFn, cancel) -> int:
    part = dest.with_suffix(dest.suffix + ".part")
    start = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={start}-"} if start else {}
    client = new_client(READ_TIMEOUT)
    try:
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code not in (200, 206):
                raise AsrModelError(
                    f"下载模型失败：HTTP {response.status_code}",
                    f"{url} 返回 {response.status_code}",
                )
            if response.status_code == 200 and start:
                start = 0  # 服务器不支持续传，从头写
            mode = "ab" if start else "wb"
            done = start
            total = done + int(response.headers.get("content-length") or 0)
            with part.open(mode) as handle:
                for chunk in response.iter_bytes(CHUNK):
                    if cancel is not None and cancel.is_set():
                        raise AsrModelError("已取消下载")
                    handle.write(chunk)
                    done += len(chunk)
                    progress(done, max(total, expected), dest.name)
    except (httpx.HTTPError, OSError) as exc:
        raise AsrModelError(
            f"下载模型失败：{exc.__class__.__name__}",
            f"{url}：{exc}",
        ) from exc
    finally:
        client.close()

    if expected and part.stat().st_size != expected:
        raise AsrModelError(
            f"下载的 {dest.name} 大小不对（{part.stat().st_size:,} 字节，应为 {expected:,}）",
            "文件可能被中途截断或被网络代理改写，请重试。",
        )
    part.replace(dest)
    return dest.stat().st_size


def download(
    *,
    folder: str | Path | None = None,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """下载全部模型文件，返回模型目录。已就绪的文件会跳过。"""
    target = Path(folder or target_dir())
    target.mkdir(parents=True, exist_ok=True)
    total = total_bytes()
    done_before = 0
    for name, size in MODEL_FILES:
        existing = target / name
        if _size_ok(existing, size):
            done_before += size

    def emit(done: int, name: str) -> None:
        if progress is not None:
            progress(done_before + done, total, name)

    for name, size in MODEL_FILES:
        dest = target / name
        if _size_ok(dest, size):
            continue
        last_error: AsrModelError | None = None
        for base in MIRRORS:
            url = f"{base}/{name}"
            try:
                _download_once(url, dest, size, lambda d, t, n: emit(d, n), cancel)
                last_error = None
                break
            except AsrModelError as exc:
                if exc.message == "已取消下载":
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
    if not is_ready(target):
        raise AsrModelError(
            "模型文件下载完成但校验不通过",
            f"请检查 {target} 下的 model.int8.onnx 与 tokens.txt。",
        )
    return target


def ensure_configured(folder: Path | None = None) -> str:
    """把可用的模型目录写进设置，返回该目录。"""
    path = str(folder or configured_dir() or "")
    if not path or not is_ready(path):
        raise AsrModelError("还没有可用的识别模型", "请先在扩展中心下载识别模型。")
    if str(settings.get("live_asr_model", "") or "") != path:
        settings.set("live_asr_model", path)
        settings.save()
    return path
