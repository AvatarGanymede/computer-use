"""Public contract-1 entrypoint. stdout is exactly one JSON object."""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.dont_write_bytecode = True
from cu.contract import EXIT_CODES, Failure
from cu.runtime import Runtime


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("internal_error", "命令行参数无效：" + message, message)

    def print_help(self, file=None):
        super().print_help(file or sys.stderr)


def parse_args(argv=None):
    parser = Parser(description="Run one Windows GUI intent using Cua Driver 0.38.x.", add_help=False)
    parser.add_argument("intent")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--budget", type=int, default=30)
    parser.add_argument("--background-only", action="store_true")
    parser.add_argument("--app", action="append", default=[])
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--artifacts-dir")
    parser.add_argument("--no-overlay", action="store_true")
    args = parser.parse_args(argv)
    import math
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.budget <= 0:
        parser.error("timeout 和 budget 必须为有限正数")
    args.timeout = min(args.timeout, 600)
    args.budget = min(args.budget, 100)
    args.intent = args.intent[:4000]
    return args


def main(argv=None):
    try:
        options = parse_args(argv)
        result = Runtime(options).run()
    except Failure as exc:
        options = SimpleNamespace(intent="", timeout=120, budget=30)
        result = Runtime(options).envelope(exc)
    except KeyboardInterrupt:
        options = SimpleNamespace(intent="", timeout=120, budget=30)
        result = Runtime(options).envelope(Failure("user_cancel", "用户已中止当前意图。"))
    except Exception as exc:
        options = SimpleNamespace(intent="", timeout=120, budget=30)
        result = Runtime(options).envelope(Failure("internal_error", str(exc), repr(exc)))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
