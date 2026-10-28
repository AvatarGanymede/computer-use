"""Fail-closed parent API for the Windows foreground guard.

Call ensure() before any foreground driver call; action() acknowledges presentation
and (for x/y targets) completion of real cursor movement before returning. The
caller owns cancellation of its driver subprocess when stop.flag appears.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable
import uuid

PROTOCOL = 1
STARTUP_TIMEOUT = 12.0
CONFIRM_TIMEOUT = 120.0
ACTION_TIMEOUT = 5.0
HEARTBEAT_TIMEOUT = 3.0


class GuardError(RuntimeError):
    """The guard cannot authorize a foreground action."""


try:
    from .guard_ipc import read_json as _read_json, write_json as _write_json
except ImportError:  # Direct module loading and the standalone guard process.
    from guard_ipc import read_json as _read_json, write_json as _write_json


def _process_command() -> list[str]:
    return [sys.executable, str(Path(__file__).with_name("guard_process.py"))]


def _spawn_options() -> dict:
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def probe() -> dict:
    """Probe a hidden window and a pass-through hook, never showing UI or moving the cursor."""
    if sys.platform != "win32":
        return {"available": False, "error": "The foreground guard requires Windows 10 2004 or later."}
    try:
        options = _spawn_options()
        options["stdout"] = subprocess.PIPE
        result = subprocess.run(_process_command() + ["--probe"], timeout=10, **options)
        payload = json.loads(result.stdout.decode("utf-8"))
        if not isinstance(payload, dict) or type(payload.get("available")) is not bool:
            raise ValueError("invalid native probe response")
        if result.returncode or not payload["available"]:
            payload["available"] = False
            payload["error"] = payload.get("error") or "Native guard probe failed."
        elif any(payload.get(key) is not True for key in ("capture_excluded", "escape_hook", "mouse_observer")):
            raise ValueError("probe did not verify capture exclusion and input observers")
        return payload
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"available": False, "error": str(exc)}


class Guard:
    """One guard per run, with serialized calls from the wrapper thread.

    stop_check is invoked while waiting and may return a truthy cancellation reason
    or raise the wrapper's cancellation exception. warn receives a string. Targets
    are physical virtual-desktop coordinates {"x": int, "y": int}, or element
    metadata without x/y. pulse arms a pass-through observer for a tagged native
    mouse-down at the requested coordinate; acknowledgment only confirms cursor
    movement. The guard never injects a click or any keyboard input.
    """

    probe = staticmethod(probe)

    def __init__(self, run_dir: Path, stop_check: Callable, warn: Callable):
        self.run_dir = Path(run_dir)
        self.stop_check = stop_check
        self.warn = warn
        self._process = None
        self._token = uuid.uuid4().hex
        self._sequence = 0
        self._ready = False
        self._ever_ready = False
        self._closed = False
        self._warnings: set[str] = set()

    @property
    def alive(self) -> bool:
        """Whether a previously authorized guard is still healthy (no cancellation callback)."""
        if not self._ever_ready:
            return False
        try:
            self.assert_ready()
            return True
        except GuardError:
            return False

    def assert_ready(self) -> None:
        """Check during driver polling; raise GuardError immediately if the child died.

        This is a no-op until the first ensure() succeeds, so it is safe inside
        stop_check while ensure() waits. It never calls stop_check recursively.
        Parent code must terminate an in-flight driver call on GuardError and map
        that failure to foreground_not_allowed. A live but stalled guard fails
        after HEARTBEAT_TIMEOUT seconds.
        """
        if not self._ever_ready:
            return
        if self._closed or self._process is None or self._process.poll() is not None:
            raise GuardError("Guard process is no longer running.")
        status = self._status()
        if status is None or not all(status.get(key) is True for key in ("capture_excluded", "escape_hook", "mouse_observer", "countdown_complete")):
            raise GuardError("Guard safety capabilities are no longer verified.")
        if (self.run_dir / "stop.flag").exists():
            try:
                reason = (self.run_dir / "stop.flag").read_text(encoding="utf-8").strip()
            except OSError:
                reason = "user_cancel"
            raise GuardError(reason or "user_cancel")

    def _check_stop(self) -> None:
        reason = self.stop_check()
        if reason:
            raise GuardError(str(reason) if isinstance(reason, str) else "Guard stopped by caller.")
        flag = self.run_dir / "stop.flag"
        if flag.exists():
            try:
                reason = flag.read_text(encoding="utf-8").strip()
            except OSError:
                reason = "user_cancel"
            raise GuardError(reason or "user_cancel")

    def _status(self) -> dict | None:
        value = _read_json(self.run_dir / "guard-status.json")
        if value is None:
            return None
        if value.get("protocol") != PROTOCOL or value.get("token") != self._token or value.get("pid") != self._process.pid or value.get("parent_pid") != os.getpid():
            return None
        if any(type(value.get(key)) is not int for key in ("protocol", "pid", "parent_pid")):
            raise GuardError("Guard identity fields have invalid types.")
        timestamp = value.get("heartbeat")
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or not -1 <= time.monotonic() - timestamp <= HEARTBEAT_TIMEOUT:
            raise GuardError("Guard heartbeat is stale or invalid.")
        messages = value.get("warnings", [])
        if not isinstance(messages, list) or any(not isinstance(message, str) for message in messages):
            raise GuardError("Guard warnings have an invalid format.")
        for message in messages:
            if message not in self._warnings:
                self._warnings.add(message)
                self.warn(message)
        if value.get("phase") == "error":
            raise GuardError(str(value.get("error") or "Guard process failed."))
        if value.get("phase") == "stopped":
            raise GuardError("Guard has stopped.")
        if value.get("phase") not in ("starting", "confirming", "countdown", "action", "ready", "stopping") or type(value.get("ack")) is not int or value["ack"] < 0:
            raise GuardError("Guard status has an invalid phase or acknowledgment.")
        return value

    def _wait(self, sequence: int, timeout: float, allow_confirmation: bool = False) -> None:
        deadline = time.monotonic() + timeout
        confirmation_deadline = None
        in_confirmation = False
        while True:
            self._check_stop()
            status = self._status()
            if self._process.poll() is not None:
                raise GuardError("Guard process exited before authorizing the action.")
            if status:
                if status.get("phase") == "confirming" and allow_confirmation:
                    if confirmation_deadline is None:
                        confirmation_deadline = time.monotonic() + CONFIRM_TIMEOUT
                    deadline = max(deadline, confirmation_deadline)
                    in_confirmation = True
                elif in_confirmation:
                    deadline = time.monotonic() + timeout
                    in_confirmation = False
                if status.get("ack") == sequence and status.get("phase") == "ready":
                    if not all(status.get(key) is True for key in ("ready", "capture_excluded", "escape_hook", "mouse_observer", "countdown_complete")):
                        raise GuardError("Guard readiness handshake is missing a required safety capability.")
                    self._check_stop()
                    self._ready = True
                    self._ever_ready = True
                    return
            if time.monotonic() >= deadline:
                raise GuardError("Timed out waiting for guard readiness or action acknowledgment.")
            time.sleep(0.01)

    def _send(self, kind: str, **values) -> int:
        self._sequence += 1
        _write_json(self.run_dir / "guard-command.json", {"protocol": PROTOCOL, "token": self._token, "seq": self._sequence, "kind": kind, **values})
        return self._sequence

    def ensure(self, window_id: int | None) -> None:
        """Start/verify the guard and finish the one-time two-second countdown."""
        if self._closed:
            raise GuardError("Guard is closed.")
        if window_id is not None and (type(window_id) is not int or not 0 < window_id < 2 ** 64):
            raise GuardError("window_id must be a positive native window handle or None.")
        try:
            self._check_stop()
            if self._process is None:
                if sys.platform != "win32":
                    raise GuardError("The foreground guard requires Windows.")
                self.run_dir.mkdir(parents=True, exist_ok=True)
                command = _process_command() + ["--run-dir", str(self.run_dir.resolve()), "--parent-pid", str(os.getpid()), "--token", self._token]
                if window_id is not None:
                    command += ["--window-id", str(window_id)]
                self._process = subprocess.Popen(command, **_spawn_options())
                self._wait(0, STARTUP_TIMEOUT, allow_confirmation=True)
            else:
                self._ready = False
                sequence = self._send("ensure", window_id=window_id)
                self._wait(sequence, ACTION_TIMEOUT, allow_confirmation=True)
        except GuardError:
            if not self._ever_ready:
                self.close()
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if not self._ever_ready:
                self.close()
            raise GuardError(str(exc)) from exc
        except BaseException:
            if not self._ever_ready:
                self.close()
            raise

    def action(self, label: str, target: dict | None = None, pulse: bool = False) -> None:
        """Present an action; a coordinate target physically moves the system cursor."""
        if not self._ready or self._closed or self._process is None:
            raise GuardError("Call ensure() successfully before action().")
        if not isinstance(label, str) or not label.strip():
            raise GuardError("Action label must be nonempty text.")
        if target is not None and not isinstance(target, dict):
            raise GuardError("Action target must be an object or None.")
        point = None
        if target is not None and ("x" in target or "y" in target):
            if "element_token" in target or any(type(target.get(key)) is not int for key in ("x", "y")):
                raise GuardError("Coordinate targets require integer x/y and no element_token.")
            point = {"x": target["x"], "y": target["y"]}
        try:
            self._check_stop()
            status = self._status()
            if self._process.poll() is not None or not status or status.get("ready") is not True:
                raise GuardError("Guard is no longer ready.")
            self._ready = False
            sequence = self._send("action", label=label[:240], target=point, pulse=bool(pulse))
            self._wait(sequence, ACTION_TIMEOUT)
        except GuardError:
            if not self._ever_ready:
                self.close()
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if not self._ever_ready:
                self.close()
            raise GuardError(str(exc)) from exc
        except BaseException:
            if not self._ever_ready:
                self.close()
            raise

    def close(self) -> None:
        """Stop the child and release its native resources; keep stop.flag for the caller."""
        self._ready = False
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    _write_json(self.run_dir / "guard-close.json", {"protocol": PROTOCOL, "token": self._token})
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError) as exc:
            self.warn(f"Could not completely stop guard process: {exc}")
