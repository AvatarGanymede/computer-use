"""Per-run files, cancellation and cleanup; no persisted run memory."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .contract import Failure
from .process import pid_alive, process_identity

SESSION_RE = re.compile(r"^cu-(?:health-)?\d{8}T\d{6}-[0-9a-f]{6}$")


def session_id(health=False):
    return f"cu-{'health-' if health else ''}{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


def runs_root() -> Path:
    return Path(tempfile.gettempdir()) / "claude-cu" / "runs"


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Preserve the write error; run cleanup will retry removing the directory.
        raise


def sweep(warn=lambda _: None, root=None):
    root = root or runs_root()
    if not root.exists():
        return
    for folder in root.iterdir():
        if not SESSION_RE.fullmatch(folder.name) or not folder.is_dir() or folder.is_symlink():
            continue
        try:
            if time.time() - folder.stat().st_mtime < 3600:
                continue
            runtime_file = folder / "runtime.json"
            try:
                runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                runtime = {}
            if not isinstance(runtime, dict):
                runtime = {}
            # A custom run retains its artifacts, not the temporary control directory.
            destination = runtime.get("artifacts_dir")
            custom = isinstance(destination, str) and bool(destination) and Path(destination).resolve() != folder.resolve()
            if runtime.get("keep") and not custom:
                continue
            pid = runtime.get("pid", 0)
            identity = runtime.get("process_identity")
            if runtime.get("phase") != "finished" and type(pid) is int and pid > 0 and isinstance(identity, str) and identity and pid_alive(pid) and process_identity(pid) == identity:
                continue
            shutil.rmtree(folder)
        except (OSError, ValueError, TypeError) as exc:
            warn(f"cleanup_failed: {folder}: {exc}")


class RunFiles:
    def __init__(self, session, *, keep=False, destination=None, warn=lambda _: None):
        self.session = session
        self.keep = keep or destination is not None
        self.warn = warn
        self.control_dir = runs_root() / session
        # Always retain a discoverable live control directory for stop.py, even with custom artifacts.
        self.path = Path(destination).expanduser().resolve() / session if destination is not None else self.control_dir
        self.runtime = {"pid": os.getpid(), "process_identity": process_identity(os.getpid()), "started_at": time.time(), "phase": "starting", "stop_requested": False, "session": session, "keep": self.keep, "artifacts_dir": str(self.path)}
        self.last_screenshot = None
        created = []
        try:
            self.path.mkdir(parents=True, exist_ok=False)
            created.append(self.path)
            if self.path != self.control_dir:
                self.control_dir.mkdir(parents=True, exist_ok=False)
                created.append(self.control_dir)
            self.update("starting")
        except BaseException:
            # The caller cannot finish an instance whose construction failed.
            for path in reversed(created):
                self._remove(path)
            raise

    def update(self, phase):
        self.runtime["phase"] = phase
        atomic_json(self.control_dir / "runtime.json", self.runtime)
        if self.path != self.control_dir:
            atomic_json(self.path / "runtime.json", self.runtime)

    def check_stop(self):
        for base in {self.path, self.control_dir}:
            flag = base / "stop.flag"
            if flag.exists():
                try:
                    reason = flag.read_text(encoding="utf-8").strip()
                except OSError:
                    reason = "user_cancel"
                raise Failure("user_escape" if reason == "user_escape" else "user_cancel", "用户已中止当前意图。")

    def append(self, record):
        with (self.path / "transcript.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def finish(self, envelope):
        try:
            if self.keep:
                envelope["artifacts"] = {"kept": True, "dir": str(self.path), "last_screenshot": str(self.last_screenshot) if self.last_screenshot else None}
            # A failed deletion must not leave the current process recorded as active.
            self.update("finished")
            if self.keep:
                atomic_json(self.path / "result.json", envelope)
        finally:
            if not self.keep:
                for path in {self.path, self.control_dir}:
                    self._remove(path)
            elif self.control_dir != self.path:
                self._remove(self.control_dir)
        return envelope

    def _remove(self, path):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.warn(f"cleanup_failed: {path}: {exc}")
