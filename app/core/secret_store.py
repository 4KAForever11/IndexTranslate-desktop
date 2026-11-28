"""用 Windows DPAPI 加密小段密钥（WebDAV 密码、私有 API Key）。

DPAPI 把密文绑定到当前 Windows 用户，同一台电脑上换进程也能解开，
复制到别的机器就解不开——正好符合「本地保存、不上传明文」的要求。
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes


class SecretError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _take(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def _crypt32():
    try:
        return ctypes.windll.crypt32
    except AttributeError as exc:  # 非 Windows
        raise SecretError("当前系统不支持 DPAPI 加密（仅 Windows）。") from exc


def protect(text: str, *, entropy: bytes = b"IndexTranslate") -> str:
    """明文 → base64 密文。空字符串原样返回。"""
    if not text:
        return ""
    crypt32 = _crypt32()
    blob_in = _blob(text.encode("utf-8"))
    blob_entropy = _blob(entropy)
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_entropy),
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretError(f"加密失败（Windows 错误码 {ctypes.GetLastError()}）。")
    return base64.b64encode(_take(blob_out)).decode("ascii")


def unprotect(cipher: str, *, entropy: bytes = b"IndexTranslate") -> str:
    """base64 密文 → 明文。解不开时抛出 SecretError，由调用方决定是否清空。"""
    if not cipher:
        return ""
    crypt32 = _crypt32()
    try:
        raw = base64.b64decode(cipher, validate=True)
    except Exception as exc:
        raise SecretError("保存的密码不是有效的密文，请重新填写。") from exc
    blob_in = _blob(raw)
    blob_entropy = _blob(entropy)
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_entropy),
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretError("无法解密保存的密码（可能是其他用户或其他电脑加密的）。")
    return _take(blob_out).decode("utf-8", "replace")