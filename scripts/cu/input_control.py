"""Session-owned Windows cancellation event. No hooks, input or daemon startup."""
from __future__ import annotations

import os
import re

from . import process
from .contract import Failure


def owner_identity():
    pid = os.getpid()
    identity = process.process_identity(pid)
    prefix = f"windows:{pid}:"
    if not isinstance(identity, str) or not identity.startswith(prefix):
        raise Failure("driver_unavailable", "无法取得 wrapper 的 Windows 进程创建身份。", retryable=False)
    try:
        created = int(identity[len(prefix):])
        if not 0 < created < 2 ** 64:
            raise ValueError("invalid creation FILETIME")
    except ValueError as exc:
        raise Failure("driver_unavailable", "wrapper 的进程创建身份无效。", identity, retryable=False) from exc
    return {"owner_pid": pid, "owner_creation_time": created}


class InputCancellation:
    def __init__(self, name):
        if not isinstance(name, str) or re.fullmatch(r"Local\\cua-driver-cancel-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", name) is None:
            raise Failure("driver_unavailable", "驱动未提供有效的会话取消事件。", retryable=False)
        if os.name != "nt":
            raise Failure("driver_unavailable", "原生输入取消需要 Windows。", retryable=False)
        import ctypes
        from ctypes import wintypes

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.OpenEventW.restype = wintypes.HANDLE
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.SetEvent.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenEventW(0x0002, False, name)
        self.signalled = False
        if not self.handle:
            raise Failure("driver_unavailable", "无法打开驱动的会话取消事件。", str(ctypes.get_last_error()), retryable=False)

    def signal(self):
        if not self.signalled:
            if not self.handle or not self.kernel.SetEvent(self.handle):
                raise Failure("driver_unavailable", "无法向原生输入循环发送会话取消信号。", retryable=False)
            self.signalled = True

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
