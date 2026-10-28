"""Normalized model observations and compact file-independent diagnostics."""
from __future__ import annotations

import ntpath


def flatten_elements(elements):
    result = []
    if isinstance(elements, dict):
        elements = list(elements.values())
    for item in elements or []:
        if not isinstance(item, dict):
            continue
        result.append(item)
        result.extend(flatten_elements(item.get("children", [])))
    return result


def normalize_window(window):
    app = window.get("app") or window.get("app_name") or window.get("process_name") or window.get("executable") or window.get("process", {}).get("name", "")
    return {"app": ntpath.basename(app), "window_title": window.get("window_title") or window.get("title", ""), "window_id": window.get("window_id") or window.get("id") or window.get("hwnd")}


def normalize_state(state, window):
    if isinstance(state.get("window"), dict):
        window = {**window, **state["window"]}
    info = normalize_window(window)
    # Native 0.38 reports current metadata at the top level, not under window.
    if "window_title" in state or "title" in state:
        info["window_title"] = state.get("window_title", state.get("title", ""))
    if "window_id" in state:
        info["window_id"] = state["window_id"]
    if state.get("app") or state.get("app_name"):
        info["app"] = ntpath.basename(state.get("app") or state["app_name"])
    raw_elements = flatten_elements(state.get("elements", []))
    elements = []
    for item in raw_elements:
        token = item.get("element_token") or item.get("token")
        actions = item.get("actions") or item.get("supported_actions") or []
        element = {"element_token": token, "role": item.get("role") or item.get("control_type", ""), "label": item.get("label") or item.get("name", ""), "actions": actions, "frame": item.get("frame") or item.get("bounds") or {}}
        for key in ("value", "automation_id", "enabled", "visible", "selected", "checked", "in_web_content", "label_from_value"):
            if key in item:
                element[key] = item[key]
        elements.append(element)
    info.update({"elements": elements, "tree_markdown": state.get("tree_markdown") or state.get("tree") or "", "screenshot_path": None})
    for key in ("screenshot_origin", "coordinate_space", "screenshot_width", "screenshot_height", "input_origin", "input_coordinate_space"):
        if key in state:
            info[key] = state[key]
    return info


def needs_screenshot(state, requested=False, retry=False):
    elements = flatten_elements(state.get("elements", []))
    useful = [item for item in elements if item.get("value") not in (None, "") or item.get("actions") or item.get("supported_actions")]
    return requested or retry or state.get("elements_complete") is False or state.get("truncated") is True or len(useful) < 5


def excerpt(observation):
    if not observation:
        return None
    lines = []
    for item in observation.get("elements", [])[:20]:
        value = item.get("value")
        lines.append(f"[{item.get('element_token', '?')}] {item.get('role', '')} {item.get('label', '')}" + (f" = {str(value)[:240]}" if value is not None else ""))
    if not lines:
        lines = str(observation.get("tree_markdown", "")).splitlines()[:20]
    return {"app": observation.get("app"), "window_title": observation.get("window_title"), "elements_excerpt": lines}


def rebind_token(token, old, fresh):
    """New snapshots may mint new tokens. Resolve only unique semantic matches."""
    if old.get("window_id") != fresh.get("window_id"):
        return None
    current = fresh.get("elements", [])
    match = next((item for item in current if item.get("element_token") == token), None)
    if match:
        return match
    original = next((item for item in old.get("elements", []) if item.get("element_token") == token), None)
    if not original:
        return None
    keys = ["role", "label"]
    if original.get("automation_id"):
        keys.append("automation_id")
    matches = [item for item in current if all(item.get(key) == original.get(key) for key in keys)]
    return matches[0] if len(matches) == 1 else None


def trusted_readback(item, prop):
    return prop in {"value", "label", "enabled", "visible", "selected", "checked"} and prop in item and not (item.get("in_web_content") is True and (prop == "value" or (prop == "label" and item.get("label_from_value") is True)))


def verify_evidence(evidence, old, fresh, windows):
    kind = evidence.get("kind")
    if kind == "dialog_gone":
        window_id = evidence.get("window_id")
        if window_id is None:
            return False, "dialog_gone 需要 window_id。"
        if window_id != old.get("window_id"):
            return False, "待验证对话框必须是此前已观察的目标。"
        return all(normalize_window(item)["window_id"] != window_id for item in windows), "已重新枚举窗口，检查目标对话框是否消失。"
    if kind == "readback":
        item = rebind_token(evidence.get("element_token"), old, fresh)
        if item is None or "expected" not in evidence:
            return False, "readback 需要可唯一定位的 element_token 和 expected。"
        prop = evidence.get("property", "value")
        if not trusted_readback(item, prop):
            return False, "指定属性缺失或来自网页 UIA 写入回显，无法独立读回；请提供截图或其他可信状态。"
        actual, expected = item[prop], evidence["expected"]
        mode = evidence.get("match", "exact")
        passed = actual == expected if mode == "exact" else isinstance(actual, str) and isinstance(expected, str) and bool(expected) and expected in actual if mode == "contains" else False
        return passed, f"重新读取 {item.get('element_token')} 的 {prop}: {str(actual)[:240]}"
    if kind == "state_change":
        if old.get("window_id") is None or old.get("window_id") != fresh.get("window_id"):
            return False, "状态变化的前后观察必须属于同一目标窗口。"
        prop = evidence.get("property")
        if prop not in {"window_title", "app", "window_id"} or "expected" not in evidence:
            return False, "state_change 需要 property（window_title/app/window_id）和 expected；元素状态使用 readback。"
        return old.get(prop) != fresh.get(prop) and fresh.get(prop) == evidence["expected"], f"重新观察窗口属性 {prop}: {fresh.get(prop)}"
    return False, "截图证据需要独立视觉校验。"
