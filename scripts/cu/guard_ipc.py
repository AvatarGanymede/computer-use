"""Atomic guard IPC with delete-sharing readers on Windows."""
import json
import os
from pathlib import Path
import time


def _open_read(path):
    if os.name != 'nt':
        return open(path, 'r', encoding='utf-8')
    import ctypes as C
    from ctypes import wintypes as W
    import msvcrt
    kernel = C.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    create.restype = W.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [W.HANDLE], W.BOOL
    handle = create(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        close(handle)
        raise
    return os.fdopen(fd, 'r', encoding='utf-8')


def read_json(path: Path):
    try:
        with _open_read(path) as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path: Path, value: dict):
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.002)
    finally:
        temporary.unlink(missing_ok=True)
