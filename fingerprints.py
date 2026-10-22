"""仅比较原始文件字节的有界SHA256缓存；不做声学识别或持久化。"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .core import MAX_SONG_UPLOAD_BYTES

CHUNK = 64 * 1024
MAX_BYTES = MAX_SONG_UPLOAD_BYTES
MAX_ENTRIES = 512


class FingerprintError(ValueError):
    """脱敏的文件指纹校验错误。"""


def _state(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _identity(value):
    # Windows 3.12的路径stat与fstat可能使用不同ctime语义；跨API只比较创建时间。
    return (*_state(value)[:4], getattr(value, "st_birthtime_ns", value.st_ctime_ns))


def _native_change_stamp(fd):
    if os.name != "nt":
        return None
    # Python 3.10在Windows上没有公开可靠的ChangeTime，不能仅凭恢复后的mtime命中缓存。
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class BasicInfo(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_longlong) for name in ("created", "accessed", "written", "changed")
        ] + [("attributes", wintypes.DWORD)]

    info = BasicInfo()
    query = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    query.restype = wintypes.BOOL
    if not query(msvcrt.get_osfhandle(fd), 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise FingerprintError("无法核对音频变化状态，未使用缓存或保存上传。")
    return info.changed


def _handle_state(handle):
    return (*_state(os.fstat(handle.fileno())), _native_change_stamp(handle.fileno()))


def _settled(handle_state):
    # NTFS快速连续写入可能暂时保留同一时间值；近期文件不信任元数据缓存。
    changed_ns = handle_state[4]
    if os.name == "nt":
        if not handle_state[-1] or handle_state[-1] <= 0:
            return False
        changed_ns = handle_state[-1] * 100 - 11644473600 * 1000000000
    return time.time_ns() - max(changed_ns, handle_state[3]) >= 3 * 1000000000


def _stream_hash(source, expected):
    digest = hashlib.sha256()
    count = 0
    while True:
        block = source.read(CHUNK)
        if not block:
            break
        count += len(block)
        if count > expected or count > MAX_BYTES:
            raise FingerprintError("音频在校验期间变化或超过安全限制，未保存上传。")
        digest.update(block)
    if count != expected:
        raise FingerprintError("音频在校验期间被截断，未保存上传。")
    return digest.hexdigest()


@dataclass(frozen=True)
class FileFingerprint:
    sha256: str
    size_bytes: int
    path_state: tuple
    handle_state: tuple


class FingerprintCache:
    """只在上传去重时使用；缓存命中仍校验文件身份，不重复读取音频。"""

    def __init__(self, *, max_entries=MAX_ENTRIES):
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_ENTRIES:
            raise ValueError("指纹缓存数量超出限制。")
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self._lock = threading.RLock()

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def forget(self, path):
        with self._lock:
            self._entries.pop(Path(path).absolute(), None)

    def fingerprint(self, path):
        path = Path(path).absolute()
        with self._lock:
            try:
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
                    raise FingerprintError("音频为空、过大、链接或不是普通文件，未保存上传。")
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                )
                fd = os.open(path, flags)
                with os.fdopen(fd, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
                        raise FingerprintError("音频在打开期间变化，未保存上传。")
                    path_state, handle_state = _state(before), _handle_state(handle)
                    cached = self._entries.get(path)
                    if (
                        cached
                        and cached.path_state == path_state
                        and cached.handle_state == handle_state
                        and _settled(handle_state)
                    ):
                        digest = cached.sha256
                    else:
                        digest = _stream_hash(handle, opened.st_size)
                    after = path.lstat()
                    if (
                        not stat.S_ISREG(after.st_mode)
                        or _state(after) != path_state
                        or _handle_state(handle) != handle_state
                    ):
                        raise FingerprintError("音频在读取期间变化，未保存上传。")
                result = FileFingerprint(digest, opened.st_size, path_state, handle_state)
                self._entries[path] = result
                self._entries.move_to_end(path)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
                return result
            except (OSError, FingerprintError) as exc:
                self._entries.pop(path, None)
                if isinstance(exc, FingerprintError):
                    raise
                raise FingerprintError(
                    "音频在校验时无法读取或发生变化，请刷新曲库后重试。"
                ) from exc
