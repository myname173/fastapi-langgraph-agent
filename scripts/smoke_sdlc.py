r"""End-to-end SDLC smoke test.

Logs in, opens a session, submits a requirement to the SDLC orchestrator,
walks the human-approval loop, then reports plan / code / test / feedback
counts and fails (exit 1) if the run produced no code.

Run from the host (API reachable on localhost):

    python scripts/smoke_sdlc.py
    python scripts/smoke_sdlc.py --email me@x.com --password 'Str0ng!Pass' \
        --requirements "写一个 Python 函数，判断字符串是否为回文，并附 pytest 测试"

Credentials default to the demo account; override with flags or the
SMOKE_EMAIL / SMOKE_PASSWORD env vars.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Bypass any ambient HTTP proxy (CI sandboxes often inject one).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_REQUIREMENTS = "用 Python 写一个函数 is_prime(n) 判断整数是否为质数，并给出 pytest 单元测试。代码尽量简短。"


def req(base, method, path, token=None, body=None, form=None, timeout=600):
    """Issue one JSON/form request; return ``(status, parsed_or_text)``."""
    headers = {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:600]


def main() -> int:
    """Run the smoke test; return a process exit code."""
    parser = argparse.ArgumentParser(description="SDLC end-to-end smoke test")
    parser.add_argument("--base-url", default=os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8010"))
    parser.add_argument("--email", default=os.getenv("SMOKE_EMAIL", "demo@test.com"))
    parser.add_argument("--password", default=os.getenv("SMOKE_PASSWORD", "Demo!Pass2026"))
    parser.add_argument("--requirements", default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--max-approvals", type=int, default=8)
    args = parser.parse_args()

    status, data = req(
        args.base_url,
        "POST",
        "/api/v1/auth/login",
        form={"email": args.email, "password": args.password, "grant_type": "password"},
    )
    if status != 200:
        print(f"login failed: HTTP {status} {data}", file=sys.stderr)
        return 1
    user_token = data["access_token"]

    status, data = req(args.base_url, "POST", "/api/v1/auth/session", token=user_token, body={})
    if status != 200:
        print(f"session failed: HTTP {status} {data}", file=sys.stderr)
        return 1
    session_token = data["token"]["access_token"]

    print("submitting requirement to /api/v1/sdlc/ ...", flush=True)
    started = time.time()
    status, data = req(
        args.base_url,
        "POST",
        "/api/v1/sdlc/",
        token=session_token,
        body={"requirements": args.requirements},
    )
    print(f"HTTP {status} in {time.time() - started:.1f}s", flush=True)
    if status != 200:
        print(data, file=sys.stderr)
        return 1

    thread_id = data["thread_id"]
    print(f"thread_id: {thread_id}")
    print(
        f"status={data.get('status')} complete={data.get('is_complete')} approval_needed={data.get('human_approval_needed')}"
    )

    for round_no in range(1, args.max_approvals + 1):
        if not data.get("human_approval_needed"):
            break
        print(f"approving (round {round_no}) ...", flush=True)
        status, data = req(args.base_url, "POST", f"/api/v1/sdlc/{thread_id}/approve", token=session_token)
        print(f"HTTP {status} status={data.get('status')} complete={data.get('is_complete')}")
        if status != 200:
            print(data, file=sys.stderr)
            return 1

    status, state = req(args.base_url, "GET", f"/api/v1/sdlc/{thread_id}/state", token=session_token)
    if status != 200:
        print(f"state failed: HTTP {status} {state}", file=sys.stderr)
        return 1

    counts = {
        key: len(state.get(key) or [])
        for key in ("plan", "code_changes", "test_results", "feedback", "security_issues", "errors")
    }
    print("\n--- result ---")
    for key, value in counts.items():
        print(f"  {key:18s}: {value}")

    passed = [t for t in (state.get("test_results") or []) if t.get("status") == "pass"]
    if passed:
        print(f"  tests passing     : {len(passed)}")

    if counts["code_changes"] == 0:
        print("\nFAIL: no code_changes produced (coder likely errored — check app logs).", file=sys.stderr)
        return 1

    print("\nOK: SDLC pipeline produced code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
