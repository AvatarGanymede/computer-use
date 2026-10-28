"""Internal HTTP transport; only this child reads its piped stdin."""
import json
import os
import sys
import urllib.error
import urllib.request


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="strict")
    request = json.load(sys.stdin)
    headers = {"Content-Type": "application/json"}
    key_name = request.get("api_key_env")
    if key_name:
        key = os.environ.get(key_name)
        if not key:
            raise ValueError(f"Missing environment variable {key_name}")
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(request["payload"], ensure_ascii=False).encode("utf-8")
    message = urllib.request.Request(request["endpoint"], data=data, headers=headers, method="POST")
    # Explicit endpoints must not silently forward UI contents to a redirect host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(message, timeout=request["timeout"]) as response:
            sys.stdout.write(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        sys.stderr.write(exc.read().decode("utf-8", errors="replace"))
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
