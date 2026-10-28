"""Public wire types, validation, and exit status mapping."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

CONTRACT = 1
EXIT_CODES = {"done": 0, "error": 1, "blocked": 2, "needs_human": 3, "timeout": 124}
REASONS = {
    "background_unavailable": ("blocked", "允许前台后重试，或改用 browser_* 工具族。", True),
    "foreground_not_allowed": ("blocked", "检查覆盖层与 ESC 守卫，或改用可后台操作的工具。", True),
    "element_stale": ("blocked", "重新观察目标窗口后重试一次。", True),
    "target_not_found": ("blocked", "打开目标应用或窗口后重试。", True),
    "ambiguous_target": ("blocked", "请用户指明目标窗口或文档。", False),
    "app_not_allowed": ("blocked", "请用户检查 policy.json 的应用策略。", False),
    "verification_failed": ("blocked", "检查结果与目标状态，必要时请用户确认。", True),
    "budget_exhausted": ("timeout", "缩小意图范围，或在上限内增加时间和轮数。", True),
    "user_escape": ("needs_human", "用户已按 ESC 中止，等待用户的新指示。", False),
    "user_cancel": ("needs_human", "用户已中止，等待用户的新指示。", False),
    "escalated_after_blocked": ("needs_human", "连续两次受阻，请用户处理现场后再继续。", False),
    "needs_auth": ("needs_human", "请用户完成登录、验证码或二次验证。", False),
    "needs_decision": ("needs_human", "请用户确认歧义或不可逆操作。", False),
    "driver_unavailable": ("error", "安装 Cua Driver 0.38.x，并运行 cua-driver serve --permission-mode standard。", True),
    "model_unavailable": ("error", "检查 model.json、模型端点和完整 JSON 响应。", True),
    "internal_error": ("error", "检查 stderr；仅排查时加 --keep-artifacts 重试。", False),
}
TOOLS = frozenset({"click", "double_click", "right_click", "type_text", "press_key", "hotkey", "scroll", "drag", "set_value", "launch_app", "invoke_menu", "verify_state", "get_window_state", "list_windows"})
EVIDENCE_KINDS = frozenset({"readback", "screenshot", "state_change", "dialog_gone"})


@dataclass
class Failure(Exception):
    reason: str
    detail: str
    raw: str = ""
    hint: str | None = None
    status: str | None = None
    retryable: bool | None = None

    def __post_init__(self):
        if self.reason not in REASONS:
            raise ValueError(f"Unknown reason: {self.reason}")
        default_status, default_hint, retryable = REASONS[self.reason]
        self.status = self.status or default_status
        self.hint = self.hint or default_hint
        self.retryable = retryable if self.retryable is None else self.retryable
        super().__init__(self.detail)

    def error(self) -> dict | None:
        if self.status in {"done", "needs_human"}:
            return None
        return {"code": self.reason, "detail": self.detail, "raw": self.raw, "retryable": self.retryable, "hint": self.hint}


def strict_json(text: str) -> Any:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    def constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def validate_response(value: Any, raw: str, max_actions: int = 5) -> dict:
    def invalid(message):
        raise Failure("model_unavailable", message, raw)

    if not isinstance(value, dict):
        invalid("模型响应必须是 JSON 对象。")
    if "contract" in value and value["contract"] != CONTRACT:
        invalid("模型 contract 主版本不兼容。")
    actions = value.get("actions", [])
    done, human = value.get("done"), value.get("ask_human")
    if not isinstance(actions, list) or len(actions) > min(max_actions, 5):
        invalid("模型动作必须为最多五项的数组。")
    if sum([bool(actions), done is not None, human is not None]) > 1:
        invalid("actions、done 与 ask_human 互斥。")
    if not isinstance(value.get("need_vision", False), bool):
        invalid("need_vision 必须为布尔值。")
    if not actions and done is None and human is None and not value.get("need_vision"):
        invalid("模型响应没有动作、完成、求助或视觉请求。")
    for action in actions:
        if not isinstance(action, dict) or action.get("tool") not in TOOLS:
            invalid("模型返回词表之外的工具。")
        args = action.get("args", {})
        if not isinstance(args, dict):
            invalid("动作 args 必须为对象。")
        # Session, mode, screenshots and script execution belong to the wrapper.
        if any(key in args for key in ("session", "permission_mode", "screenshot_out_file", "since", "steps", "script", "input_cancellation_event")):
            invalid("模型不能覆盖会话、驱动模式、截图路径或批处理。")
        target = action.get("target")
        if "target" in action:
            if not isinstance(target, dict):
                invalid("target 必须是元素 token 或屏幕坐标。")
            if set(target) == {"element_token"}:
                if not isinstance(target["element_token"], str) or not target["element_token"]:
                    invalid("element_token 必须是非空字符串。")
            elif set(target) == {"x", "y"}:
                if any(type(target[key]) is not int for key in ("x", "y")):
                    invalid("屏幕坐标必须为整数。")
            else:
                invalid("target 只能给 element_token 或 x/y，不能混用。")
        if any(key in args for key in ("element_token", "target", "x", "y")):
            invalid("元素或点击坐标必须放在 target，不能通过 args 绕过目标检查。")
        if args.get("foreground_fallback") is True:
            invalid("模型不能开启驱动自动前台降级。")
    if done is not None:
        if not isinstance(done, dict) or not isinstance(done.get("summary"), str) or not done["summary"].strip():
            invalid("done 必须包含非空 summary。")
        evidence = done.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("kind") not in EVIDENCE_KINDS or not isinstance(evidence.get("detail"), str) or not evidence["detail"].strip():
            invalid("done 必须包含有效 evidence.kind 与 detail。")
    if human is not None:
        if not isinstance(human, dict) or human.get("reason") not in {"needs_auth", "needs_decision"} or not isinstance(human.get("message"), str) or not human["message"].strip():
            invalid("ask_human 必须给出 needs_auth/needs_decision 和 message。")
    if value.get("expect") is not None and not isinstance(value["expect"], dict):
        invalid("expect 必须是对象或 null。")
    return value
