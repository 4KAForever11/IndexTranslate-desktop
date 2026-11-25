"""Image loading, EXIF-aware compression and data-URL encoding.

Encoding a large screenshot can take a noticeable moment, so this module is
only called from worker threads (never from the UI thread).
"""

from __future__ import annotations

import base64
import io
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import IMAGE_MAX_BYTES, IMAGE_MAX_SIDE

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

SUPPORTED_FILTER = "图片 (*.png *.jpg *.jpeg *.webp *.gif *.bmp)"


@dataclass
class ImagePayload:
    data_url: str
    display_name: str
    width: int
    height: int
    byte_size: int
    compressed: bool
    source: str
    mime: str


def _mime_for(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("image/"):
        return mime
    return _MIME_BY_EXT.get(Path(path).suffix.lower(), "image/png")


def _data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _load_pillow(raw: bytes):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(raw))
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:
        pass
    return image


def _prepare_local(source: str, max_side: int, max_bytes: int) -> ImagePayload:
    path = Path(source)
    if not path.is_file():
        raise ValueError(f"图片文件不存在：{source}")

    raw = path.read_bytes()
    if not raw:
        raise ValueError("图片文件为空。")

    mime = _mime_for(str(path))
    try:
        image = _load_pillow(raw)
    except Exception as exc:
        raise ValueError("无法读取该图片，请换一张 PNG / JPG 图片。") from exc

    width, height = image.size
    animated = getattr(image, "is_animated", False)
    needs_resize = max(width, height) > max_side
    needs_recompress = len(raw) > max_bytes

    if animated and not needs_resize and not needs_recompress:
        return ImagePayload(_data_url(raw, mime), path.name, width, height, len(raw), False, source, mime)

    if needs_resize or needs_recompress:
        if needs_resize:
            image.thumbnail((max_side, max_side))
        width, height = image.size
        buffer = io.BytesIO()
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        if has_alpha:
            image.convert("RGBA").save(buffer, format="PNG", optimize=True)
            mime_out = "image/png"
        else:
            image.convert("RGB").save(buffer, format="JPEG", quality=88, optimize=True)
            mime_out = "image/jpeg"
        encoded = buffer.getvalue()
        return ImagePayload(
            _data_url(encoded, mime_out),
            path.name,
            width,
            height,
            len(encoded),
            True,
            source,
            mime_out,
        )

    return ImagePayload(_data_url(raw, mime), path.name, width, height, len(raw), False, source, mime)


def prepare_image(
    source: str,
    *,
    max_side: int = IMAGE_MAX_SIDE,
    max_bytes: int = IMAGE_MAX_BYTES,
) -> ImagePayload:
    """Load a local file or pass an http(s)/data URL straight through."""
    source = (source or "").strip()
    if not source:
        raise ValueError("没有选择图片。")

    if source.startswith(("http://", "https://", "data:image/")):
        mime = "image/png"
        if source.startswith("data:image/") and ";" in source:
            mime = source[5:].split(";", 1)[0]
        name = source.split("?")[0].rsplit("/", 1)[-1] or "网络图片"
        size = len(source)
        return ImagePayload(source, name, 0, 0, size, False, source, mime)

    return _prepare_local(source, max_side, max_bytes)


def prepare_qimage(qimage) -> ImagePayload:
    """Encode a clipboard QImage (screenshot paste) as a normalised data URL."""
    from PyQt6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not qimage.save(buffer, "PNG"):
        raise ValueError("无法读取剪贴板中的图片。")
    raw = bytes(buffer.data())
    buffer.close()

    image = _load_pillow(raw)
    width, height = image.size
    if max(width, height) > IMAGE_MAX_SIDE:
        image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        width, height = image.size
        out = io.BytesIO()
        image.convert("RGBA").save(out, format="PNG", optimize=True)
        raw = out.getvalue()

    return ImagePayload(
        _data_url(raw, "image/png"),
        "剪贴板图片",
        width,
        height,
        len(raw),
        False,
        "",
        "image/png",
    )


def fetch_remote_image(
    url: str,
    *,
    max_side: int = IMAGE_MAX_SIDE,
    max_bytes: int = IMAGE_MAX_BYTES,
) -> ImagePayload:
    """Download a remote image and encode it like a local one.

    The public endpoint rejects remote references ("不支持远程资源地址"), so a
    URL input has to be fetched locally and turned into a data URL first.
    """
    from .api_client import new_client

    if not url.startswith(("http://", "https://")):
        raise ValueError("请输入以 http:// 或 https:// 开头的图片链接。")

    try:
        with new_client(60.0) as client:
            response = client.get(url, follow_redirects=True)
            response.raise_for_status()
            raw = response.content
    except Exception as exc:
        raise ValueError(f"图片下载失败：{exc}") from exc

    if not raw:
        raise ValueError("图片下载失败：服务器返回了空内容。")

    mime = (response.headers.get("content-type") or "").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = _mime_for(url.split("?")[0])

    name = url.split("?")[0].rsplit("/", 1)[-1] or "远程图片"

    try:
        image = _load_pillow(raw)
    except Exception as exc:
        raise ValueError("链接指向的不是可读取的图片。") from exc

    width, height = image.size
    animated = getattr(image, "is_animated", False)
    needs_resize = max(width, height) > max_side
    needs_recompress = len(raw) > max_bytes

    if animated and not needs_resize and not needs_recompress:
        return ImagePayload(_data_url(raw, mime), name, width, height, len(raw), False, url, mime)

    if needs_resize or needs_recompress:
        if needs_resize:
            image.thumbnail((max_side, max_side))
        width, height = image.size
        buffer = io.BytesIO()
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        if has_alpha:
            image.convert("RGBA").save(buffer, format="PNG", optimize=True)
            mime_out = "image/png"
        else:
            image.convert("RGB").save(buffer, format="JPEG", quality=88, optimize=True)
            mime_out = "image/jpeg"
        encoded = buffer.getvalue()
        return ImagePayload(
            _data_url(encoded, mime_out),
            name,
            width,
            height,
            len(encoded),
            True,
            url,
            mime_out,
        )

    return ImagePayload(_data_url(raw, mime), name, width, height, len(raw), False, url, mime)


def make_thumbnail_from_bytes(raw: bytes | str, *, max_side: int = 320) -> Optional[bytes]:
    """Small preview built from already-encoded image bytes or a data URL."""
    if isinstance(raw, str):
        marker = "base64,"
        if marker not in raw:
            return None
        try:
            raw = base64.b64decode(raw.split(marker, 1)[1])
        except Exception:
            return None
    if not raw:
        return None
    try:
        image = _load_pillow(raw)
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        if image.mode in ("RGBA", "LA", "P"):
            image.convert("RGBA").save(buffer, format="PNG", optimize=True)
        else:
            image.convert("RGB").save(buffer, format="JPEG", quality=80, optimize=True)
        return buffer.getvalue()
    except Exception:
        return None


def make_thumbnail(source: str, *, max_side: int = 320) -> Optional[bytes]:
    """Small PNG preview stored with history entries."""
    source = (source or "").strip()
    if not source or not os.path.isfile(source):
        return None
    try:
        image = _load_pillow(Path(source).read_bytes())
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        if image.mode in ("RGBA", "LA", "P"):
            image.convert("RGBA").save(buffer, format="PNG", optimize=True)
        else:
            image.convert("RGB").save(buffer, format="JPEG", quality=80, optimize=True)
        return buffer.getvalue()
    except Exception:
        return None


def make_thumbnail_from_qimage(qimage, *, max_side: int = 320) -> Optional[bytes]:
    """Small PNG preview for a clipboard QImage."""
    from PyQt6.QtCore import QBuffer, QIODevice, Qt

    if qimage is None or qimage.isNull():
        return None
    try:
        scaled = qimage.scaled(
            max_side,
            max_side,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not scaled.save(buffer, "PNG"):
            return None
        data = bytes(buffer.data())
        buffer.close()
        return data
    except Exception:
        return None


def human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes / 1024 / 1024:.1f} MB"
