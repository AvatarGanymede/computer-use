"""Start the driver if needed, then probe without sending desktop input."""
import argparse
import json
import platform
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
from cu.artifacts import session_id
from cu.contract import CONTRACT, Failure
from cu.model import Model
from cu.observation import flatten_elements, normalize_window


def health(root=None, driver_factory=None, model_factory=Model, overlay_probe=None):
    root = root or Path(__file__).resolve().parents[1]
    sid = session_id(health=True)
    result = {"contract": CONTRACT, "ok": False, "driver": {"version": None, "running": False, "permission_mode": None, "binary": None}, "model": {"adapter": None, "reachable": False, "name": None, "probe_latency_ms": None}, "smoke": {"session": sid, "list_windows": "not_run", "get_window_state": "not_run", "token_fresh": False}, "overlay": {"available": False}, "python": platform.python_version(), "warnings": []}
    deadline = time.monotonic() + 90

    def check():
        if time.monotonic() >= deadline:
            raise Failure("budget_exhausted", "health 探测超过 90 秒。")

    driver = None
    try:
        if driver_factory is None:
            from cu.driver import Driver
            driver_factory = Driver
        driver = driver_factory(sid, check=check, remaining=lambda: max(.01, deadline - time.monotonic()))
        result["driver"] = driver.inspect()
        result["driver"] = driver.ensure_started()
        if not result["driver"].get("running") or result["driver"].get("permission_mode") != "standard":
            raise Failure("driver_unavailable", "驱动未能以 standard 模式就绪。")
        driver.require_capabilities()
        windows = driver.list_windows()
        result["smoke"]["list_windows"] = "ok"
        candidates = [w for w in windows if normalize_window(w).get("window_id")]
        if not candidates:
            raise Failure("target_not_found", "没有可供健康检查的窗口。")
        state = driver.observe(candidates[0])
        result["smoke"]["get_window_state"] = "ok"
        # Resolve a returned token through a read-only token-aware API, not token string inspection.
        tokens = [x.get("element_token") or x.get("token") for x in flatten_elements(state.get("elements", []))]
        tokens = [x for x in tokens if x]
        if hasattr(driver, "probe_token") and tokens:
            result["smoke"]["token_fresh"] = driver.probe_token(candidates[0], tokens[0])
        else:
            result["warnings"].append("token_probe_unavailable: 无可用元素 token 或只读 token 探测接口，未证明 token 新鲜性。")
    except Exception as exc:
        result["warnings"].append(str(exc))
    finally:
        if driver and hasattr(driver, "close") and result["driver"].get("running"):
            try:
                driver.close()
            except Exception as exc:
                result["warnings"].append(f"driver_session_cleanup_failed: {exc}")
    try:
        model = model_factory(root / "model.json", check=check, remaining=lambda: max(.01, deadline - time.monotonic()))
        result["model"].update({"adapter": model.info["adapter"], "name": model.info["name"]})
        start = time.monotonic()
        model.probe()
        result["model"].update({"reachable": True, "probe_latency_ms": round((time.monotonic() - start) * 1000)})
    except Exception as exc:
        result["warnings"].append(str(exc))
    try:
        if overlay_probe is None:
            from cu.guard import probe
            overlay_probe = probe
        result["overlay"] = overlay_probe()
    except Exception as exc:
        result["overlay"] = {"available": False, "error": str(exc)}
    result["ok"] = all([result["driver"].get("running"), result["driver"].get("permission_mode") == "standard", result["model"]["reachable"], result["smoke"]["list_windows"] == "ok", result["smoke"]["get_window_state"] == "ok", result["smoke"]["token_fresh"], result["overlay"].get("available")])
    return result


def main():
    from run import Parser
    parser = Parser(add_help=False)
    parser.add_argument("--json", action="store_true")
    try:
        parser.parse_args()
        result = health()
    except Exception as exc:
        result = {"contract": CONTRACT, "ok": False, "warnings": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
