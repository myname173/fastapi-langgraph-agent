"""Tool routing accuracy test for the chat investment advisor.

Sends predefined queries through the chat API and detects which tools
the LLM agent actually called (via SSE stream + app log file).
Computes routing accuracy against expected tools.

Run inside the app container:
    docker exec <app> /app/.venv/bin/python /app/scripts/test_tool_routing.py
"""

import os
import re
import time

import requests

BASE_URL = "http://localhost:8000/api/v1"
# Credentials come from the environment so no secret is committed.
#   export SMOKE_EMAIL="investor_7k3m@example.com" SMOKE_PASSWORD="..."
EMAIL = os.environ.get("SMOKE_EMAIL", "investor_7k3m@example.com")
PASSWORD = os.environ["SMOKE_PASSWORD"]
LOG_DIR = "/app/logs"

# All possible tool names to search for in SSE and logs
TOOL_NAMES = [
    "fetch_realtime_quote",
    "fetch_fundamentals",
    "fetch_kline",
    "fetch_news_sentiment",
    "generate_chart",
    "research_debate",
    "search_knowledge_base",
    "ask_human",
    "duckduckgo_search",
]

# Test queries: (query_text, expected_tool_or_None)
# None = no tool should be called (casual chit-chat)
QUERIES = [
    ("查一下600519的实时行情", "fetch_realtime_quote"),
    ("分析一下600519的基本面", "fetch_fundamentals"),
    ("画一下600519近30天的K线", "fetch_kline"),
    ("600519最近有什么新闻", "fetch_news_sentiment"),
    ("对比亚迪做多空研究辩论", "research_debate"),
    ("你好呀", None),
    ("今天天气怎么样", None),
    ("帮我查一下茅台的股价", "fetch_realtime_quote"),
]


def login() -> str:
    """Login and return user access token."""
    resp = requests.post(
        f"{BASE_URL}/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "grant_type": "password"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_session(user_token: str) -> str:
    """Create a chat session and return session access token."""
    resp = requests.post(
        f"{BASE_URL}/auth/session",
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]["access_token"]


def send_chat(session_token: str, message: str) -> tuple[str, list[str]]:
    """Send a chat message and return (full_sse_text, tools_detected).

    Streams the SSE response, collects all text, and searches for
    tool call indicators in both the SSE data and the app log file.
    """
    before = time.time()

    try:
        resp = requests.post(
            f"{BASE_URL}/chatbot/chat",
            headers={
                "Authorization": f"Bearer {session_token}",
                "Content-Type": "application/json",
            },
            json={"messages": [{"role": "user", "content": message}]},
            stream=True,
            timeout=90,
        )
    except Exception as e:
        return f"[ERROR] {e}", []

    sse_lines = []
    for line in resp.iter_lines():
        if line:
            sse_lines.append(line.decode("utf-8", errors="replace"))

    sse_text = "\n".join(sse_lines)

    # Search SSE for tool names
    tools_detected = []
    for tool in TOOL_NAMES:
        if tool in sse_text:
            tools_detected.append(tool)

    # Also check recent app log entries — but ONLY entries written after
    # this query was sent (avoid cumulative contamination from earlier
    # queries in the same session).
    time.sleep(1)  # let logs flush
    log_tools = _check_logs_for_tools_since(before)
    for tool in log_tools:
        if tool not in tools_detected:
            tools_detected.append(tool)

    return sse_text, tools_detected


def _check_logs_for_tools_since(since_timestamp: float) -> list[str]:
    """Search app log files for tool call events written AFTER the given timestamp.

    Only reads lines with timestamps newer than ``since_timestamp`` to avoid
    picking up tool calls from earlier queries in the same session.
    """
    tools_found = []
    if not os.path.isdir(LOG_DIR):
        return tools_found

    # Regex to match ISO-8601 timestamps at the start of log lines, for example
    # a line beginning with 2026-08-04T10:46:00.272240Z followed by [info].
    ts_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")

    for fname in os.listdir(LOG_DIR):
        fpath = os.path.join(LOG_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = ts_pattern.match(line)
                    if not m:
                        continue
                    line_ts = m.group(1)
                    try:
                        line_epoch = time.mktime(time.strptime(line_ts, "%Y-%m-%dT%H:%M:%S"))
                    except ValueError:
                        continue
                    if line_epoch < since_timestamp:
                        continue
                    for tool in TOOL_NAMES:
                        if tool in line and tool not in tools_found:
                            tools_found.append(tool)
        except (OSError, PermissionError):
            pass

    return tools_found


def main():
    """Run the tool-routing accuracy suite and print a summary."""
    print("=" * 70)
    print("  Tool Routing Accuracy Test")
    print("=" * 70)
    print(f"  Queries: {len(QUERIES)}")

    print("\n  Logging in...")
    user_token = login()
    print("  Creating session...")
    session_token = create_session(user_token)
    print("  Ready.\n")

    results = []
    for i, (query, expected) in enumerate(QUERIES):
        # Casual-chat queries (expected=None) get a FRESH session to avoid
        # context contamination from prior financial queries in the same
        # session. In production, a new user saying "你好" has no financial
        # history, so this mirrors real usage.
        if expected is None:
            session_token = create_session(user_token)

        print(f"  [{i + 1}/{len(QUERIES)}] Q: {query}")
        print(f"         Expected: {expected or 'no tool'}")

        sse_text, tools_called = send_chat(session_token, query)

        if expected is None:
            correct = len(tools_called) == 0
        else:
            correct = expected in tools_called

        status = "PASS" if correct else "FAIL"
        print(f"         Called:   {tools_called or 'none'}")
        print(f"         [{status}]\n")

        results.append((query, expected, tools_called, correct))
        time.sleep(2)

    # Summary
    correct_count = sum(1 for _, _, _, c in results if c)
    accuracy = correct_count / len(results)

    print(f"{'=' * 70}")
    print(f"  Tool Routing Accuracy: {correct_count}/{len(results)} = {accuracy * 100:.1f}%")
    print(f"{'=' * 70}")
    for query, expected, actual, correct in results:
        mark = "PASS" if correct else "FAIL"
        exp_str = expected or "none"
        act_str = ", ".join(actual) if actual else "none"
        print(f"  [{mark}] {query[:30]:30s} exp={exp_str:25s} got={act_str}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
