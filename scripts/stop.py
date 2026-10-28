"""Human-operated cancellation; no application processes are killed."""
import json
import os
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
from cu.artifacts import SESSION_RE, runs_root, session_id, sweep
from cu.contract import CONTRACT
from cu.process import kill_tree, pid_alive, process_identity


def same_process(runtime):
    identity = runtime.get("process_identity")
    return bool(identity and pid_alive(runtime["pid"]) and process_identity(runtime["pid"]) == identity)


def running_sessions(root=None):
    found = []
    root = root or runs_root()
    if not root.exists():
        return found
    for folder in root.iterdir():
        if not SESSION_RE.fullmatch(folder.name) or folder.is_symlink():
            continue
        try:
            runtime = json.loads((folder / "runtime.json").read_text(encoding="utf-8"))
            if not isinstance(runtime, dict):
                continue
            if runtime.get("phase") != "finished" and type(runtime.get("pid")) is int and runtime["pid"] != os.getpid() and same_process(runtime):
                found.append((folder, runtime))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(found, key=lambda entry: entry[1].get("started_at", 0), reverse=True)


def stop(args, *, root=None, wait_sec=5):
    warnings = []
    result = {"contract": CONTRACT, "ok": True, "stopped": [], "forced": False}
    found = running_sessions(root)
    if args.session:
        found = [entry for entry in found if entry[0].name == args.session]
    elif not args.all:
        found = found[:1]
    for folder, runtime in found:
        try:
            (folder / "stop.flag").write_text("user_cancel", encoding="utf-8")
            destination = Path(runtime["artifacts_dir"])
            if destination != folder and destination.is_dir():
                (destination / "stop.flag").write_text("user_cancel", encoding="utf-8")
        except OSError as exc:
            warnings.append(str(exc))
    deadline = time.monotonic() + wait_sec
    while any(same_process(runtime) for _, runtime in found) and time.monotonic() < deadline:
        time.sleep(.05)
    for folder, runtime in found:
        if same_process(runtime):
            # Re-read metadata and bind termination to the OS process creation identity.
            try:
                current = json.loads((folder / "runtime.json").read_text(encoding="utf-8"))
                if current.get("pid") != runtime["pid"] or current.get("process_identity") != runtime.get("process_identity") or current.get("phase") == "finished":
                    raise ValueError("运行记录已变化，跳过强制终止。")
                if not kill_tree(runtime["pid"], expected_identity=runtime["process_identity"]):
                    raise ValueError("进程身份无法确认或终止失败，未强制结束。")
                result["forced"] = True
                warnings.append("driver_stop_unconfirmed: 已终止 wrapper；不能据此确认 daemon 在途输入已结束。")
            except (OSError, ValueError) as exc:
                result["ok"] = False
                warnings.append(str(exc))
                continue
        if same_process(runtime):
            result["ok"] = False
            warnings.append(f"仍在运行：{folder.name}")
        else:
            result["stopped"].append(folder.name)
            if not runtime.get("keep"):
                import shutil
                for path in {folder, Path(runtime["artifacts_dir"])}:
                    if path.exists():
                        try:
                            shutil.rmtree(path)
                        except OSError as exc:
                            warnings.append(str(exc))
    if args.driver:
        try:
            from cu.driver import Driver
            Driver(session_id()).stop_daemon()
        except Exception as exc:
            result["ok"] = False
            warnings.append(str(exc))
    sweep(warnings.append, root)
    if warnings:
        result["warnings"] = warnings
    return result


def main():
    from run import Parser
    parser = Parser(add_help=False)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--session")
    choice.add_argument("--all", action="store_true")
    parser.add_argument("--driver", action="store_true")
    parser.add_argument("--json", action="store_true")
    try:
        result = stop(parser.parse_args())
    except Exception as exc:
        result = {"contract": CONTRACT, "ok": False, "stopped": [], "forced": False, "warnings": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
