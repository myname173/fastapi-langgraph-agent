"""Focused test for the coder agent node (SDLC flow).

Logs in with the existing test account, starts an SDLC run with a small,
well-scoped feature request, then polls the run until it reaches the
coder/coding phase (or finishes) and reports what the coder produced.

Run from host:
    uv run python scripts/test_coder.py
"""

import json
import os
import sys
import time

import requests

BASE_URL = "http://localhost:8000/api/v1"
# Credentials come from the environment so no secret is committed.
#   export SMOKE_EMAIL="investor_7k3m@example.com" SMOKE_PASSWORD="..."
EMAIL = os.environ.get("SMOKE_EMAIL", "investor_7k3m@example.com")
PASSWORD = os.environ["SMOKE_PASSWORD"]

# A small, self-contained feature request so coder has bounded scope.
TEST_REQUEST = (
    "在 app/utils/ 下新增一个函数 sanitize_filename(name: str) -> str，"
    "它把任意字符串转换为安全的文件名：去掉路径分隔符、控制字符和空格，"
    "只保留字母数字、下划线、短横线和点号，长度限制 100 字符，返回纯 ASCII。"
)


def login() -> str:
    """Log in and return a bearer token."""
    resp = requests.post(
        f"{BASE_URL}/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "grant_type": "password"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def main() -> int:
    """Exercise the coder chat path end to end."""
    token = login()
    print("login_ok")
    user_headers = {"Authorization": f"Bearer {token}"}
    # SDLC requires a session token, not the user token.
    s = requests.post(f"{BASE_URL}/auth/session", headers=user_headers, timeout=15)
    print("session_status", s.status_code)
    s.raise_for_status()
    session_token = s.json()["token"]["access_token"]
    print("session_ok")
    headers = {"Authorization": f"Bearer {session_token}"}
    payload = {"requirements": TEST_REQUEST}
    r = requests.post(f"{BASE_URL}/sdlc/", json=payload, headers=headers, timeout=600)
    print("sdlc_post_status", r.status_code)
    if r.status_code != 200:
        print("sdlc_post_body", r.text[:1000])
        return 1
    data = r.json()
    run_id = data.get("run_id") or data.get("id")
    print("run_id", run_id)
    # Poll for the run to progress; coder is reached after specing+planning.
    for _ in range(40):
        time.sleep(10)
        g = requests.get(f"{BASE_URL}/sdlc/{run_id}", headers=headers, timeout=30)
        if g.status_code != 200:
            print("get_status", g.status_code, g.text[:500])
            continue
        gd = g.json()
        phase = gd.get("current_phase") or gd.get("phase") or gd.get("status")
        print("poll_phase", phase, "step", gd.get("current_step"), "progress", gd.get("progress"))
        if phase in ("coding", "testing", "reviewing", "completed", "failed", "coder"):
            # Dump the coder-related artifacts.
            print("=== SDLC STATE (key fields) ===")
            for k in (
                "current_phase",
                "status",
                "result",
                "coder_output",
                "code_changes",
                "generated_code",
                "plan",
                "spec",
                "messages",
            ):
                if k in gd:
                    val = gd[k]
                    s = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
                    print(f"--- {k} ---")
                    print(s[:2000])
            if phase in ("completed", "failed"):
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
