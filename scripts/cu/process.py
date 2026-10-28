"""Bounded, cancellable subprocesses. The wrapper never reads its stdin."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .contract import Failure


def _windows_kernel():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _windows_identity(kernel, handle, pid):
    import ctypes
    from ctypes import wintypes

    creation, exit_time, system, user = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(system), ctypes.byref(user)):
        return None
    ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return f"windows:{pid}:{ticks}" if ticks else None


def process_identity(pid: int) -> str | None:
    """OS birth identity, never a liveness guess. Missing/unsupported identity fails closed."""
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        kernel = _windows_kernel()
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            return _windows_identity(kernel, handle, pid)
        finally:
            kernel.CloseHandle(handle)
    try:
        # comm may contain spaces and ')'; field 22 is starttime since boot.
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = stat[stat.rindex(")") + 2:].split()
        start = int(suffix[19])
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if not boot or start < 0 or int(stat.split("(", 1)[0]) != pid:
            return None
        return f"linux:{pid}:{boot}:{start}"
    except (OSError, ValueError, IndexError):
        return None


def kill_tree(pid: int, expected_identity: str | None = None) -> bool:
    """Kill an owned tree; persisted PIDs require a matching OS identity.

    On Windows a held root handle pins its PID until taskkill has finished. POSIX
    cannot atomically bind killpg to a birth identity, so checked group kills fail closed.
    """
    if type(pid) is not int or pid <= 0:
        return False
    if expected_identity is not None and (not isinstance(expected_identity, str) or not expected_identity):
        return False
    if os.name == "nt":
        kernel = handle = None
        try:
            if expected_identity is not None:
                kernel = _windows_kernel()
                # QUERY_LIMITED_INFORMATION | SYNCHRONIZE; hold across the PID-based operation.
                handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
                if not handle or _windows_identity(kernel, handle, pid) != expected_identity:
                    return False
                if kernel.WaitForSingleObject(handle, 0) != 0x102:
                    return False
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            if handle:
                kernel.CloseHandle(handle)
    if expected_identity is not None:
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False


def run_process(command: list[str], *, timeout: float, check: Callable[[], None] = lambda: None, input_text: str | None = None, env: dict | None = None, on_abort: Callable[[], None] = lambda: None, kill_descendants: bool = True) -> subprocess.CompletedProcess:
    check()
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env, **kwargs)
    deadline = time.monotonic() + max(0.01, timeout)
    completed = threading.Event()
    exchange = {}
    worker = None

    def communicate():
        try:
            exchange["output"] = process.communicate(input=input_text)
        except BaseException as exc:
            exchange["error"] = exc
        finally:
            completed.set()

    try:
        if input_text is not None:
            # Python 3.12 on Windows can block writing stdin inside communicate,
            # before its timeout takes effect. Keep all pipe I/O off the polling thread.
            worker = threading.Thread(target=communicate, daemon=True, name="cu-model-pipes")
            worker.start()
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            if worker is not None:
                if not completed.wait(min(0.05, remaining)):
                    continue
                if "error" in exchange:
                    raise exchange["error"]
                out, err = exchange["output"]
            else:
                try:
                    out, err = process.communicate(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
            check()
            return subprocess.CompletedProcess(command, process.returncode, out, err)
    except BaseException as exc:
        # Signal the native session before potentially slow CLI tree termination.
        try:
            on_abort()
        except BaseException as abort_error:
            exc.add_note(f"Cancellation notification failed: {abort_error}")
        try:
            if kill_descendants:
                kill_tree(process.pid)
            else:
                process.kill()
        except BaseException as kill_error:
            exc.add_note(f"CLI tree termination failed: {kill_error}")
            try:
                process.kill()
            except BaseException:
                pass
        try:
            if worker is None:
                process.communicate(timeout=1)
            else:
                process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                if worker is None:
                    process.communicate(timeout=1)
                else:
                    process.wait(timeout=1)
            except BaseException as reap_error:
                exc.add_note(f"CLI reap failed: {reap_error}")
        except BaseException as reap_error:
            exc.add_note(f"CLI reap failed: {reap_error}")
        finally:
            if worker is not None:
                try:
                    worker.join(timeout=0.2)
                except BaseException as join_error:
                    exc.add_note(f"Pipe worker join failed: {join_error}")
        raise


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
