"""Windows 本地密钥保护；非 Windows 环境使用兼容回退。"""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


def _dpapi(data: bytes, decrypt: bool) -> bytes:
    if os.name != "nt":
        return data
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    raw = ctypes.create_string_buffer(data)
    inp = Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("Windows 数据保护 API 调用失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(value: str) -> str:
    return base64.b64encode(_dpapi(value.encode("utf-8"), False)).decode("ascii") if value else ""


def unprotect(value: str) -> str:
    return _dpapi(base64.b64decode(value), True).decode("utf-8") if value else ""
