"""Cua Driver 0.38.x adapter with explicit initial startup and guarded input."""
from __future__ import annotations

import copy
import json
import math
import ntpath
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import daemon, process
from .contract import Failure, REASONS, TOOLS, strict_json, validate_response
from .input_control import InputCancellation, owner_identity

BATCH_TOOLS = frozenset({"click", "double_click", "right_click", "set_value", "type_text", "press_key", "hotkey", "scroll", "drag", "move_cursor"})
START_HINT = "检查 native/bin 的 Cua Driver 0.38.x 补丁版是否完整，或用 CU_DRIVER_BINARY 指定；已有 daemon 必须为 standard 且支持所需原生能力。"


def _find_binary(binary=None):
    override = os.environ.get("CU_DRIVER_BINARY") or binary
    if override:
        # An explicit missing override must not silently select a different executable.
        return str(Path(os.path.expandvars(str(override))).expanduser())
    root = Path(__file__).resolve().parents[2]
    names = ("cua-driver.exe", "cua-driver") if os.name == "nt" else ("cua-driver",)
    directories = [root / "native" / "bin", Path(sys.executable).parent]
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    found = shutil.which("cua-driver")
    if found:
        return found
    for directory in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    raise Failure("driver_unavailable", "未找到 cua-driver；可用 CU_DRIVER_BINARY 指定安装位置。", hint=START_HINT)


def _process_path(pid):
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        return path.value if kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)) else None
    finally:
        kernel.CloseHandle(handle)


def _window_origin(pid, window_id):
    """Exact inverse of Windows 0.38 bitmap_to_screen, in physical pixels."""
    if os.name != "nt":
        raise Failure("driver_unavailable", "屏幕坐标转换目前只实现 Windows 0.38。")
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL
    user.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
    user.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    owner = wintypes.DWORD()
    if not user.GetWindowThreadProcessId(window_id, ctypes.byref(owner)) or owner.value != pid:
        raise Failure("target_not_found", "坐标目标窗口不存在或已不属于指定进程。")
    previous = user.SetThreadDpiAwarenessContext(wintypes.HANDLE(-4))
    if not previous:
        raise Failure("driver_unavailable", "无法建立物理像素 DPI 上下文，不能可靠转换坐标。")
    try:
        rect = wintypes.RECT()
        if dwm.DwmGetWindowAttribute(window_id, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
            return rect.left + 1, rect.top + 1
        if user.GetWindowRect(window_id, ctypes.byref(rect)):
            return rect.left, rect.top
        raise Failure("target_not_found", "无法读取目标窗口的屏幕位置。")
    finally:
        user.SetThreadDpiAwarenessContext(previous)


def _validate(value, schema, path="args"):
    """Validate the JSON-schema subset actually emitted by the 0.38 tool registry."""
    kind = schema.get("type")
    checks = {"object": lambda: isinstance(value, dict), "array": lambda: isinstance(value, list), "string": lambda: isinstance(value, str), "integer": lambda: type(value) is int, "number": lambda: type(value) in (int, float) and math.isfinite(value), "boolean": lambda: type(value) is bool, "null": lambda: value is None}
    if kind in checks and not checks[kind]():
        raise ValueError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} has an unsupported value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise ValueError(f"{path} missing {', '.join(sorted(missing))}")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError(f"{path} has unknown fields: {', '.join(sorted(value.keys() - properties.keys()))}")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{path}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise ValueError(f"{path} has invalid length")
        for i, item in enumerate(value):
            _validate(item, schema.get("items", {}), f"{path}[{i}]")
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise ValueError(f"{path} has invalid length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValueError(f"{path} has invalid format")
    elif type(value) in (int, float):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(f"{path} is outside the permitted range")


def _failure(value, raw, fallback="internal_error"):
    error = value.get("refusal") or value.get("error") or value
    if not isinstance(error, dict):
        error = {"message": str(error)}
    code = str(error.get("code") or value.get("code") or "")
    aliases = {"stale_element_token": "element_stale", "stale_element": "element_stale", "stale_snapshot": "element_stale", "window_not_found": "target_not_found", "app_not_found": "target_not_found", "process_not_found": "target_not_found", "ambiguous_element": "ambiguous_target", "foreground_unavailable": "foreground_not_allowed", "connection_refused": "driver_unavailable", "daemon_unavailable": "driver_unavailable", "daemon_not_running": "driver_unavailable", "input_cancellation_mismatch": "driver_unavailable", "invalid_batch": "model_unavailable", "invalid_arguments": "model_unavailable"}
    reason = aliases.get(code, code if code in REASONS else fallback)
    detail = str(error.get("message") or error.get("detail") or value.get("message") or value.get("detail") or code or "驱动调用失败。")
    if not code:
        if "stale_element_token" in detail or "stale element token" in detail.lower():
            reason = "element_stale"
        elif "background_unavailable" in detail:
            reason = "background_unavailable"
        elif any(text in detail.lower() for text in ("daemon is not running", "connection refused", "failed to connect", "cannot connect", "broken pipe")):
            reason = "driver_unavailable"
    hint = error.get("hint") or value.get("hint") or (START_HINT if reason == "driver_unavailable" else None)
    exc = Failure(reason, detail, raw, hint=hint, retryable=False if code == "input_cancellation_mismatch" else None)
    exc.retries_used = 0
    exc.action_retries_used = []
    return exc


def _is_failure(value):
    return value.get("ok") is False or value.get("status") in {"refused", "error", "failed"} or bool(value.get("code")) or bool(value.get("error")) or value.get("isError") is True


class Driver:
    def __init__(self, session, check=lambda: None, remaining=lambda: 600, binary=None):
        if not isinstance(session, str) or not session or session == "default" or session.startswith("__cua_runtime_"):
            raise ValueError("session must be an explicit non-reserved public label")
        self.session = session
        self.check = check
        self.remaining = remaining
        self.binary = _find_binary(binary)
        self._version = None
        self._startup_attempted = False
        self._startup_deadline = None
        self._capabilities = None
        self._schemas = {}
        self._last_raw = ""
        self._cancel_requested = threading.Event()
        self._session_established = False
        self._stop_result = None
        self.stop_confirmed = False
        self._input_control = None
        self._input_event_name = None
        self.cancellation_delivered = False
        self.cancellation_owner_bound = False

    def _check_running(self):
        if self._cancel_requested.is_set():
            raise Failure("user_cancel", "驱动会话已请求结束，拒绝继续投递。", retryable=False)
        self.check()
        if self._startup_deadline is not None and time.monotonic() >= self._startup_deadline:
            raise Failure("driver_unavailable", "等待 Cua Driver 自动启动超过 15 秒。", hint=START_HINT, retryable=False)

    def _run(self, *args):
        self._check_running()
        timeout = min(600, self.remaining())
        if self._startup_deadline is not None:
            timeout = min(timeout, self._startup_deadline - time.monotonic())
        if timeout <= 0:
            raise Failure("budget_exhausted", "调用驱动前运行时间已耗尽。")
        try:
            result = process.run_process([self.binary, *args], timeout=timeout, check=self._check_running, on_abort=self.request_stop)
            self._check_running()
            return result
        except subprocess.TimeoutExpired as exc:
            self.check()
            if self._startup_deadline is not None and time.monotonic() >= self._startup_deadline:
                raise Failure("driver_unavailable", "等待 Cua Driver 自动启动超过 15 秒。", hint=START_HINT, retryable=False) from exc
            raw = exc.stdout or ""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            raise Failure("budget_exhausted", "等待驱动时超过运行期限。", raw) from exc
        except OSError as exc:
            raise Failure("driver_unavailable", f"无法执行驱动：{exc}", str(exc), hint=START_HINT) from exc

    def _ensure_version(self):
        if self._version is None:
            result = self._run("--version")
            version = result.stdout.strip()
            if result.returncode or not re.fullmatch(r"cua-driver 0\.38\.\d+", version):
                raise Failure("driver_unavailable", "只支持稳定版 Cua Driver 0.38.x。", result.stdout or result.stderr, hint=START_HINT)
            self._version = version
        return self._version

    def inspect(self):
        version = self._ensure_version()
        result = self._run("status")
        text = result.stdout + result.stderr
        if result.returncode and "daemon is not running" not in text.lower():
            raise Failure("driver_unavailable", "无法读取驱动 daemon 状态。", text, hint=START_HINT)
        running = result.returncode == 0 and "daemon is not running" not in text.lower()
        mode = re.search(r"^\s*permission mode:\s*(standard|bounded|unrestricted)(?:\s|$)", text, re.MULTILINE)
        return {"version": version, "running": running, "permission_mode": mode.group(1) if running and mode else None, "binary": self.binary}

    def ensure_started(self):
        """Initial run/health preflight; calls and cleanup never use this method."""
        self._check_running()
        self._startup_deadline = time.monotonic() + 15
        try:
            info = self.inspect()
            if not info["running"]:
                if self._session_established or self._input_event_name is not None or self._startup_attempted:
                    raise Failure("driver_unavailable", "本次会话不允许重新启动 daemon。", retryable=False)
                self._startup_attempted = True
                with daemon.startup_lock(self._check_running):
                    # Another wrapper may have finished starting it while we waited.
                    info = self.inspect()
                    if not info["running"]:
                        daemon.start(self.binary, check=self._check_running, remaining=lambda: min(self.remaining(), self._startup_deadline - time.monotonic()))
                        while True:
                            self._check_running()
                            info = self.inspect()
                            if info["running"]:
                                break
                            time.sleep(min(0.05, max(0, self._startup_deadline - time.monotonic())))
            if info["permission_mode"] != "standard":
                raise Failure("driver_unavailable", "已有驱动必须为 standard 模式；不会自动重配或停止。", json.dumps(info, ensure_ascii=False), hint=START_HINT, retryable=False)
            return info
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._check_running()
            raise Failure("driver_unavailable", f"无法自动启动 Cua Driver：{exc}", str(exc), hint=START_HINT, retryable=False) from exc
        finally:
            self._startup_deadline = None

    def _ensure_ready(self):
        info = self.inspect()
        if not info["running"] or info["permission_mode"] != "standard":
            raise Failure("driver_unavailable", "驱动必须已运行且 permission mode 明确为 standard。", json.dumps(info, ensure_ascii=False), hint=START_HINT)

    def require_capabilities(self):
        if self._capabilities is None:
            value = self.call("computer_use_capabilities", {})
            required = ("session_input_cancellation", "read_only_token_probe", "screen_physical_coordinates", "direct_input_cancellation_event", "cancellation_owner_process", "cancellation_event_continuity")
            if value.get("revision") != 1 or any(value.get(key) is not True for key in required):
                raise Failure("driver_unavailable", "驱动缺少本 skill 所需的原生取消、token 探测或绝对坐标能力。", self._last_raw, hint=START_HINT, retryable=False)
            self._capabilities = value
        return copy.deepcopy(self._capabilities)

    def bind_input_cancellation(self):
        identity = owner_identity()
        value = self.call("bind_input_cancellation", identity)
        if value.get("bound") is not True or any(type(value.get(key)) is not int or value[key] != expected for key, expected in identity.items()):
            raise Failure("driver_unavailable", "驱动未确认 wrapper 进程身份绑定。", self._last_raw, retryable=False)
        name = value.get("cancellation_event")
        if self._input_event_name is not None and name != self._input_event_name:
            raise Failure("driver_unavailable", "驱动会话取消通道已改变，拒绝向新会话继续投递。", self._last_raw, retryable=False)
        if self._input_control is None:
            self._input_control = InputCancellation(name)
            self._input_event_name = name
        self.cancellation_owner_bound = True

    def request_stop(self):
        """Signal the native loop before any blocking CLI kill/reap; never reset it."""
        self._cancel_requested.set()
        if self._input_control is not None:
            self._input_control.signal()
            self.cancellation_delivered = True

    def release_input_control(self):
        if self._input_control is not None:
            self._input_control.close()
            self._input_control = None

    def probe_token(self, window, token):
        self.require_capabilities()
        value = self.call("probe_element_token", {**self._window(window), "element_token": token})
        return value.get("token_fresh") is True and value.get("element_token") == token and value.get("window_id") == window["window_id"] and value.get("pid") == window["pid"]

    def _tool_schema(self, tool):
        if tool not in self._schemas:
            self._ensure_version()
            result = self._run("describe", tool)
            try:
                if result.returncode:
                    raise ValueError("describe failed")
                schema = strict_json(result.stdout.split("input_schema:", 1)[1].strip())
                if not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
                    raise ValueError("input_schema is not an object schema")
            except (ValueError, IndexError) as exc:
                raise Failure("driver_unavailable", f"无法读取 {tool} 的真实参数 schema。", result.stdout or result.stderr) from exc
            self._schemas[tool] = schema
        return self._schemas[tool]

    @property
    def schema(self):
        # Preserve actual native schemas; the wrapper target convention is documented separately.
        schemas = {tool: copy.deepcopy(self._tool_schema(tool)) for tool in sorted(TOOLS)}
        for schema in schemas.values():
            for field in ("coordinate_space", "input_cancellation_event"):
                schema["properties"].pop(field, None)
        return schemas

    def call(self, tool, args):
        if not isinstance(args, dict):
            raise Failure("internal_error", "驱动参数必须为对象。")
        self._check_running()
        if not self._session_established and tool not in {"start_session", "end_session", "get_session", "list_sessions", "list_windows", "get_window_state", "get_desktop_state", "check_permissions", "health_report", "get_guide"}:
            # An unknown session's end_session also reports active:false. Establish
            # the lifecycle before any mutation so an end racing first admission
            # leaves a tombstone that rejects the delayed action, not a no-op.
            started = self.call("start_session", {})
            if started.get("active") is not True or started.get("session") != self.session:
                raise Failure("driver_unavailable", "无法确认驱动会话已建立。", self._last_raw, retryable=False)
            self._session_established = True
        self._ensure_ready()
        payload = copy.deepcopy(args)
        payload.pop("input_cancellation_event", None)
        payload["session"] = self.session
        if self._input_event_name is not None and tool not in {"start_session", "end_session", "bind_input_cancellation"}:
            payload["input_cancellation_event"] = self._input_event_name
        result = self._run("call", tool, json.dumps(payload, ensure_ascii=False, allow_nan=False))
        self._last_raw = result.stdout
        try:
            value = strict_json(result.stdout)
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
        except ValueError as exc:
            raw = result.stdout or result.stderr
            raise _failure({"message": raw or str(exc)}, raw, "driver_unavailable") from exc
        if tool == "run_steps":
            self._check_batch(value, result.stdout, len(payload.get("steps", [])))
        if result.returncode or _is_failure(value):
            raise _failure(value, result.stdout, "driver_unavailable" if result.returncode else "internal_error")
        if tool in {"list_windows", "get_window_state", "get_desktop_state"}:
            self._session_established = True
        return value

    @staticmethod
    def _batch_retries(value, total):
        retries = [0] * total
        rows = value.get("steps", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and type(row.get("index")) is int and 0 <= row["index"] < total:
                    # 0.38 writes refound before invoking a second time, even if
                    # that second call fails or a later expectation fails.
                    retries[row["index"]] = max(retries[row["index"]], int("refound" in row))
        return retries

    @staticmethod
    def _check_batch(value, raw, total):
        retries = Driver._batch_retries(value, total)
        rows = value.get("steps", [])
        if not isinstance(rows, list):
            exc = Failure("driver_unavailable", "批处理 steps 不是数组，不能安全确定重试后缀。", raw, retryable=False)
            exc.failed_action_index, exc.completed_count = 0, 0
            exc.retries_used = 0
            exc.action_retries_used = retries
            raise exc
        failure_index = value.get("failed_step")
        failed = next((row for row in rows if isinstance(row, dict) and row.get("ok") is False), None)
        if failed is not None or _is_failure(value):
            index = failed.get("index") if failed is not None else failure_index
            index = index if type(index) is int and 0 <= index < total else 0
            completed = 0
            for row in rows:
                if not isinstance(row, dict) or row.get("index") != completed or row.get("ok") is not True or row.get("skipped"):
                    break
                completed += 1
            phase = failed.get("phase", "action") if failed is not None else "validation"
            if phase == "expect" and completed == index:
                completed += 1  # The action ran; only its postcondition failed.
            exc = _failure(failed if failed is not None else value, raw)
            exc.failed_action_index = index
            exc.completed_count = completed
            exc.failure_phase = phase
            exc.action_retries_used = retries
            exc.retries_used = retries[index] if index < len(retries) else 0
            if phase == "expect" or exc.retries_used:
                exc.retryable = False
            raise exc
        if value.get("ok") is not True or len(rows) != total or any(not isinstance(row, dict) or row.get("ok") is not True or row.get("index") != i or row.get("skipped") for i, row in enumerate(rows)):
            exc = Failure("driver_unavailable", "批处理响应缺少可靠的逐步执行结果，不能安全重放。", raw, retryable=False)
            exc.failed_action_index = 0
            exc.completed_count = 0
            exc.action_retries_used = retries
            exc.retries_used = retries[0] if retries else 0
            raise exc
        # A successful action may close its own window. Keep a failed tail read
        # as observation data; the next full read/independent check decides effect.

    def list_windows(self):
        result = self.call("list_windows", {})
        windows = result.get("windows")
        if not isinstance(windows, list) or any(not isinstance(item, dict) for item in windows):
            raise Failure("driver_unavailable", "list_windows 未返回 windows 数组。", self._last_raw)
        normalized = []
        for item in windows:
            row = dict(item)
            pid = row.get("pid") or row.get("process_id")
            path = row.get("process_path") or row.get("executable") or _process_path(pid)
            app = path or row.get("app") or row.get("process_name") or row.get("app_name") or ""
            row.update({"app": ntpath.basename(app), "window_title": row.get("window_title") or row.get("title", ""), "window_id": row.get("window_id", row.get("id", row.get("hwnd"))), "pid": pid})
            if path:
                row.setdefault("process_path", path)
            normalized.append(row)
        return normalized

    @staticmethod
    def _window(window, args=None):
        args = args or {}
        window = window or {}
        pid, window_id = window.get("pid"), window.get("window_id")
        if type(pid) is not int or pid <= 0 or type(window_id) is not int or window_id <= 0:
            raise Failure("target_not_found", "动作需要明确的 pid 和 window_id。")
        if args.get("pid", pid) != pid or args.get("window_id", window_id) != window_id:
            raise Failure("model_unavailable", "模型不能覆盖 wrapper 已选定的 pid/window_id。")
        return {"pid": pid, "window_id": window_id}

    def observe(self, window, screenshot: Path | None = None, screenshot_only=False, desktop=False, options=None):
        if (screenshot_only or desktop) and screenshot is None:
            raise Failure("internal_error", "截图观察必须指定 screenshot 文件路径。")
        if desktop:
            return self.call("get_desktop_state", {"screenshot_out_file": str(screenshot), "max_image_dimension": 0})
        args = {**{key: value for key, value in (options or {}).items() if key in {"query", "max_elements", "max_depth", "full_output"}}, **self._window(window), "tree_format": "elements", "include_screenshot": screenshot is not None, "include_accessibility_tree": not screenshot_only, "max_image_dimension": 0}
        if screenshot is not None:
            args["screenshot_out_file"] = str(screenshot)
        # since is deliberately absent; null is valid only in run_steps.observe.
        state = self.call("get_window_state", args)
        if screenshot is not None:
            # This is the driver's INPUT origin, not a claim about every capture backend's geometry.
            state["input_origin"] = dict(zip(("x", "y"), _window_origin(args["pid"], args["window_id"])))
            state["input_coordinate_space"] = "screen_physical_pixels"
        return state

    def _prepare(self, action, window, foreground):
        tool = action["tool"]
        args = copy.deepcopy(action.get("args", {}))
        schema = self._tool_schema(tool)
        properties = schema["properties"]
        if "x" in args or "y" in args or "foreground_fallback" in args or "coordinate_space" in args:
            raise Failure("model_unavailable", "坐标须放在 target，模型不能控制 foreground_fallback。")
        if args.get("scope", "window") != "window" or args.get("from_zoom", False) or "capture_id" in args:
            raise Failure("model_unavailable", "动作必须使用当前窗口的屏幕绝对坐标；不能覆盖 scope、zoom 或 capture_id。")
        if args.get("delivery_mode") == "foreground" and not foreground:
            raise Failure("foreground_not_allowed", "只有调用方明确授权 foreground 后才允许前台投递。")
        if tool not in {"launch_app", "list_windows"}:
            args.update(self._window(window, args))
        if "delivery_mode" in properties:
            args["delivery_mode"] = "foreground" if foreground else "background"
        if tool == "get_window_state":
            args.update({"include_screenshot": False, "include_accessibility_tree": True, "tree_format": "elements", "max_image_dimension": 0})
        elif tool == "verify_state":
            args["include_screenshot"] = False
        target = action.get("target") or {}
        if set(target) & set(args):
            raise Failure("model_unavailable", "target 与 args 不能重复指定目标。")
        if tool == "drag" and set(target) == {"x", "y"}:
            if "from_x" in args or "from_y" in args:
                raise Failure("model_unavailable", "drag 的 target 与 from_x/from_y 不能同时指定。")
            args.update({"from_x": target["x"], "from_y": target["y"]})
        else:
            args.update(target)
        args["session"] = self.session
        if "x" in args or "from_x" in args:
            if "coordinate_space" not in properties:
                raise Failure("driver_unavailable", f"{tool} 缺少绝对屏幕坐标能力。", hint=START_HINT, retryable=False)
            args["coordinate_space"] = "screen_physical_pixels"
        try:
            _validate(args, schema)
            if ("x" in args) != ("y" in args):
                raise ValueError("x and y must be paired")
            if "element_token" in args and "x" in args:
                raise ValueError("element_token and coordinates are mutually exclusive")
        except ValueError as exc:
            raise Failure("model_unavailable", f"{tool} 参数不符合驱动 schema：{exc}", json.dumps(action, ensure_ascii=False)) from exc
        return {"tool": tool, "args": args}

    @staticmethod
    def _has_coordinates(step):
        return "x" in step["args"] or "from_x" in step["args"]

    def _convert_coordinates(self, step):
        # The patched driver maps physical pixels at native dispatch, without a capture.
        return copy.deepcopy(step)

    def validate_actions(self, actions, windows_for_actions, foreground_for_actions):
        """Validate an entire turn against caller-selected scopes without GUI I/O.

        Both context sequences have one entry per action. The caller must resolve
        windows from its permitted enumeration; this method never selects targets,
        observes windows, converts coordinates, or dispatches a native call.
        """
        index = 0
        try:
            raw = json.dumps(actions, ensure_ascii=False, allow_nan=False)
            validate_response({"actions": actions}, raw)
            if len(windows_for_actions) != len(actions) or len(foreground_for_actions) != len(actions):
                raise ValueError("each action needs one selected window and foreground flag")
            prepared = []
            for index, (action, window, foreground) in enumerate(zip(actions, windows_for_actions, foreground_for_actions)):
                if window is not None and not isinstance(window, dict):
                    raise ValueError("selected window must be an object or None")
                if type(foreground) is not bool:
                    raise ValueError("foreground authorization must be boolean")
                prepared.append(self._prepare(action, window, foreground))
            return prepared
        except (ValueError, TypeError) as exc:
            failure = Failure("model_unavailable", f"动作不是有效 JSON 或作用目标无效：{exc}", repr(actions))
            failure.failed_action_index, failure.completed_count = index, 0
            raise failure from exc
        except Failure as exc:
            exc.failed_action_index, exc.completed_count = index, 0
            raise

    def execute(self, actions, window, foreground=False, retry_allowed=True):
        if not actions:
            return {"ok": True, "completed_count": 0, "results": [], "action_retries_used": []}
        prepared = self.validate_actions(actions, [window] * len(actions), [foreground] * len(actions))
        self.bind_input_cancellation()
        completed = 0
        results = []
        retries = [0] * len(prepared)
        while completed < len(prepared):
            start = completed
            step = prepared[start]
            try:
                self._check_running()
                if step["tool"] in BATCH_TOOLS and retry_allowed:
                    end = start + 1
                    scope = (step["args"].get("pid"), step["args"].get("window_id"))
                    # A coordinate action can move the window. Resolve each such origin immediately before dispatch.
                    if not self._has_coordinates(step):
                        while end < len(prepared) and prepared[end]["tool"] in BATCH_TOOLS and not self._has_coordinates(prepared[end]) and (prepared[end]["args"].get("pid"), prepared[end]["args"].get("window_id")) == scope:
                            end += 1
                    steps = [self._convert_coordinates(step)] if self._has_coordinates(step) else copy.deepcopy(prepared[start:end])
                    batch = {"pid": scope[0], "window_id": scope[1], "delivery_mode": "foreground" if foreground else "background", "foreground_fallback": False, "steps": steps}
                    if end == len(prepared):
                        batch["observe"] = {"since": None, "include_screenshot": False, "tree_format": "elements", "max_image_dimension": 0}
                    result = self.call("run_steps", batch)
                    retries[start:end] = self._batch_retries(result, end - start)
                    completed = end
                else:
                    # run_steps can refind/replay internally. A wrapper retry must
                    # call the single native action to avoid a second hidden retry.
                    direct = self._convert_coordinates(step) if self._has_coordinates(step) else step
                    result = self.call(direct["tool"], direct["args"])
                    if step["tool"] == "verify_state" and (result.get("status") != "satisfied" or result.get("stable") is not True):
                        raise Failure("verification_failed", "verify_state 未稳定确认目标状态。", self._last_raw, retryable=False)
                    completed += 1
                results.append(result)
            except Failure as exc:
                exc.failed_action_index = start + getattr(exc, "failed_action_index", 0)
                exc.completed_count = start + getattr(exc, "completed_count", 0)
                local_retries = getattr(exc, "action_retries_used", [])
                retries[start:start + len(local_retries)] = local_retries
                exc.action_retries_used = retries
                exc.retries_used = getattr(exc, "retries_used", 0)
                raise
        return {"ok": True, "completed_count": completed, "results": results, "action_retries_used": retries}

    def cancel(self, timeout=5):
        """Request session termination and confirm native quiescence within a separate budget.

        The patched 0.38 driver checks session termination between input units.
        Cleanup waits for all native child workers, including timed-out UIA work.
        Failure means stop is UNCONFIRMED, never success.
        """
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        try:
            self.request_stop()
        except Failure:
            pass  # The independent end_session transport may still deliver cancellation.
        if self.stop_confirmed:
            return copy.deepcopy(self._stop_result)
        deadline = time.monotonic() + timeout
        cleanup = Driver(self.session, remaining=lambda: max(0, deadline - time.monotonic()), binary=self.binary)
        cleanup._version = self._version
        last_raw = ""
        while time.monotonic() < deadline:
            try:
                value = cleanup.call("end_session", {})
                last_raw = cleanup._last_raw
                if value.get("session") != self.session or value.get("active") is not False or value.get("cleanup_complete") is False or value.get("cleanup_in_progress") is True:
                    raise Failure("driver_unavailable", "end_session 没有明确确认当前会话已停止。", last_raw, retryable=False)
                self.stop_confirmed = True
                self.cancellation_delivered = True
                self.release_input_control()
                self._stop_result = {**value, "stop_confirmed": True}
                return copy.deepcopy(self._stop_result)
            except Failure as exc:
                last_raw = exc.raw
                try:
                    pending = strict_json(last_raw)
                except (ValueError, TypeError):
                    pending = {}
                if not isinstance(pending, dict) or pending.get("code") not in {"session_cleanup_pending", "session_cleanup_partial"} or pending.get("session") != self.session:
                    exc.stop_confirmed = False
                    exc.retryable = False
                    raise
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        exc = Failure("driver_unavailable", "等待会话结束超时；原生在途动作可能仍在执行，未确认输入停止。", last_raw, retryable=False)
        exc.stop_confirmed = False
        raise exc

    def close(self, timeout=5):
        """Close only after the same confirmation required for cancellation."""
        return self.cancel(timeout=timeout)

    def stop_daemon(self):
        self._ensure_version()
        result = self._run("stop")
        if result.returncode:
            raise Failure("driver_unavailable", "停止驱动 daemon 失败。", result.stdout or result.stderr)
        return {"ok": True, "message": result.stdout.strip()}
