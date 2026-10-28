"""Best-effort user policy, explicitly not a Windows sandbox."""
from __future__ import annotations

import copy
import fnmatch
import ntpath
import os
from pathlib import Path

from .contract import Failure, strict_json

DEFAULT_POLICY = {"version": 1, "apps": {"default": "allow", "deny": ["keepass.exe", "1password.exe"]}, "tools_denied": ["run_script", "kill_app", "clipboard_read", "start_recording", "replay_trajectory"], "clipboard": {"read": "deny", "write": "allow"}, "files": {"write_allow": ["%USERPROFILE%\\Documents\\**"]}, "foreground": "allow", "max_runtime_sec": 600}


class Policy:
    def __init__(self, path: Path, warn=lambda _: None):
        if not path.exists():
            self.data = copy.deepcopy(DEFAULT_POLICY)
            warn("policy_missing: 使用内置默认策略；standard 模式不是安全沙箱。")
        else:
            try:
                self.data = strict_json(path.read_text(encoding="utf-8"))
                value = self.data
                if not isinstance(value, dict) or value.get("version") != 1 or value.get("foreground") not in {"allow", "deny"}:
                    raise ValueError("version/foreground 无效")
                if value["apps"]["default"] not in {"allow", "deny"} or not isinstance(value["apps"].get("deny"), list):
                    raise ValueError("apps 策略无效")
                if type(value["max_runtime_sec"]) not in {int, float} or not 0 < value["max_runtime_sec"] <= 600:
                    raise ValueError("max_runtime_sec 无效")
                for entries in (value["apps"]["deny"], value.get("tools_denied", []), value.get("files", {}).get("write_allow", [])):
                    if not isinstance(entries, list) or not all(isinstance(x, str) for x in entries):
                        raise ValueError("策略列表必须是字符串数组")
                if value.get("clipboard", {}).get("read") != "deny" or value.get("clipboard", {}).get("write") not in {"allow", "deny"}:
                    raise ValueError("clipboard.read 必须 deny，write 必须 allow/deny")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise Failure("internal_error", f"策略文件无效：{exc}", str(exc)) from exc

    def allows_app(self, name):
        if not name:
            return False
        name = ntpath.basename(name).casefold()
        apps = self.data["apps"]
        if any(fnmatch.fnmatchcase(name, pattern.casefold()) for pattern in apps.get("deny", [])):
            return False
        return apps["default"] == "allow" or any(fnmatch.fnmatchcase(name, pattern.casefold()) for pattern in apps.get("allow", []))

    def check_app(self, name):
        if not self.allows_app(name):
            raise Failure("app_not_allowed", f"目标应用 {name or '(未知)'} 不在允许范围内。")

    def check_action(self, action):
        if action["tool"] in self.data.get("tools_denied", []):
            raise Failure("app_not_allowed", f"动作 {action['tool']} 被策略禁止。")
        args = action.get("args", {})
        if action["tool"] == "launch_app":
            if args.get("urls"):
                raise Failure("app_not_allowed", "urls 由系统默认处理程序投递，无法证明目标应用符合策略；请明确启动受允许的应用。", retryable=False)
            # Match the driver's real launch precedence, not an ignored decoy field.
            bundle = args.get("bundle_id")
            packaged_bundle = bundle if isinstance(bundle, str) and "!" in bundle else None
            identity = args.get("launch_path") or args.get("path") or args.get("aumid") or packaged_bundle or args.get("name") or bundle
            if identity is None:
                raise Failure("model_unavailable", "launch_app 需要 name/path/aumid/bundle_id/launch_path。")
            if not isinstance(identity, str) or not identity.strip():
                raise Failure("model_unavailable", "launch_app 目标必须为非空字符串。")
            if args.get("launch_path"):
                # Match Windows split_launchable_target before checking the executable.
                identity = identity.strip()
                if not identity.startswith("shell:") and "!" not in identity:
                    if identity.startswith('"') and '"' in identity[1:]:
                        identity = identity[1:].split('"', 1)[0]
                    elif not identity.startswith('"'):
                        first = identity.split(maxsplit=1)[0]
                        if first.casefold().endswith(".exe"):
                            identity = first
            self.check_app(identity)
        # Direct file destinations are checked; GUI Save dialogs remain advisory model constraints.
        for field in ("write_path", "save_path", "output_path"):
            if field in args:
                target = ntpath.normcase(ntpath.abspath(os.path.expandvars(args[field])))
                patterns = self.data.get("files", {}).get("write_allow", [])
                if not any(fnmatch.fnmatchcase(target, ntpath.normcase(os.path.expandvars(pattern))) for pattern in patterns):
                    raise Failure("app_not_allowed", f"写入路径 {args[field]} 不符合 files.write_allow。")
