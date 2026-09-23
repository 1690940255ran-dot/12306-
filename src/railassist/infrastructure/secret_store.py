"""操作系统凭据保护（设计文档 6）。

Windows DPAPI 加密会话数据；密钥与当前 Windows 用户绑定。
注意：同一 Windows 用户下的进程仍可能解密这些数据，界面需明确说明该限制。
"""
import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from railassist.domain.errors import CapabilityUnavailable

_DESCRIPTION = "RailAssist"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise CapabilityUnavailable("会话加密保存仅支持 Windows（DPAPI）。")
    crypt32 = ctypes.windll.crypt32
    blob_in = _DATA_BLOB(
        len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), _DESCRIPTION, None, None, None, 0, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise CapabilityUnavailable("DPAPI 加解密失败（会话数据可能损坏或属于其他用户）。")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(data: bytes) -> bytes:
    return _dpapi(data, protect=True)


def unprotect(data: bytes) -> bytes:
    return _dpapi(data, protect=False)


class SecretStore:
    """会话等敏感数据的本地加密落盘；密钥不进入数据库或配置文件。"""

    def __init__(self, directory: Path):
        self.directory = directory

    @property
    def path(self) -> Path:
        return self.directory / "session.bin"

    def save(self, data: bytes) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        blob = protect(data)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.path)
        return self.path

    def load(self) -> bytes | None:
        if not self.path.exists():
            return None
        try:
            return unprotect(self.path.read_bytes())
        except CapabilityUnavailable:
            # 属于其他用户或已损坏：按无会话处理，删除防止反复报错
            self.clear()
            return None

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
