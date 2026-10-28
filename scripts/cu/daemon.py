"""Initial daemon startup only; the short-lived launcher isolates its process tree."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def startup_lock(check):
    """Serialize skill starters across Windows sessions; access errors fail closed."""
    import ctypes
    from ctypes import wintypes

    if os.name != "nt":
        raise OSError("Automatic Cua Driver startup requires Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, "Global\\computer-use-driver-start-v1")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        while True:
            check()
            status = kernel.WaitForSingleObject(handle, 0)
            if status in (0, 0x80):  # An abandoned mutex is acquired by this thread too.
                acquired = True
                check()
                yield
                return
            if status != 0x102:
                raise OSError(f"Daemon startup mutex wait failed: {status}")
            time.sleep(0.05)
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def start(binary, *, check, remaining):
    from . import process
    from .contract import Failure, strict_json

    # Only this launcher may be killed on abort. A daemon it has already created
    # can be shared by another wrapper and must survive cancellation of this run.
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "--spawn", binary]
    result = process.run_process(command, timeout=min(10, remaining()), check=check, kill_descendants=False)
    try:
        value = strict_json(result.stdout)
        if result.returncode or not isinstance(value, dict):
            raise ValueError("launcher failed")
        if value.get("started") is not False or value.get("pending") is not True:
            if type(value.get("pid")) is not int or value["pid"] <= 0:
                raise ValueError("launcher did not return a daemon PID or pending marker")
    except ValueError as exc:
        raise Failure("driver_unavailable", "无法在后台启动 Cua Driver。", result.stderr or result.stdout, retryable=False) from exc


def _namespace(binary):
    # Match the pinned driver's canonical-path channel selection in bundle.rs.
    path = Path(shutil.which(binary) or binary).resolve()
    local = path.name in {"cua-driver-local", "cua-driver-local.exe"} or "CuaDriverLocal.app" in path.parts
    return "cua-driver-local" if local else "cua-driver"


@contextmanager
def _startup_marker(binary):
    """The daemon retains this handle even if its launching wrapper exits pre-bind."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    namespace = _namespace(binary)
    ctypes.set_last_error(0)
    handle = kernel.CreateEventW(None, True, False, f"Global\\computer-use-driver-lifetime-{namespace}-v1")
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == 183:  # ERROR_ALREADY_EXISTS: an earlier launch is still alive.
            yield None
        else:
            os.set_handle_inheritable(handle, True)
            yield handle
    finally:
        kernel.CloseHandle(handle)


def _spawn(binary):
    if os.name != "nt":
        raise OSError("Automatic Cua Driver startup requires Windows")
    with _startup_marker(binary) as marker:
        if marker is None:
            print(json.dumps({"started": False, "pending": True}), flush=True)
            return
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [marker]}
        child = subprocess.Popen([binary, "serve", "--permission-mode", "standard"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, startupinfo=startup, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        print(json.dumps({"pid": child.pid}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--spawn":
        sys.exit(2)
    try:
        _spawn(sys.argv[2])
    except OSError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
