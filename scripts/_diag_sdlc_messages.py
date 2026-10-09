"""Diagnostic: dump an SDLC thread's message history and validate tool-call pairing.

Use this when an SDLC agent fails with a provider 400 such as
"Messages with role 'tool' must be a response to a preceding message with
'tool_calls'" — it prints the exact history the agent sends (after the
``prepare_sdlc_messages`` trim + sanitise pass) and flags any tool message
whose ``tool_call_id`` was never declared by a preceding assistant turn.

Find the thread id (it equals ``sdlc_task.thread_id``):

    docker exec <db-container> psql -U myuser -d mydb \
        -c "SELECT id, status, created_at FROM sdlc_task ORDER BY created_at DESC LIMIT 5;"

Then run inside the app container:

    MSYS_NO_PATHCONV=1 docker exec -e PYTHONPATH=/app -e THREAD_ID=<id> \
        fastapi-langgraph-agent-production-ready-template-master-app-1 \
        /app/.venv/bin/python /app/scripts/_diag_sdlc_messages.py
"""

import asyncio
import json
import os

import app.main  # noqa: F401  (import first to avoid circular-import ordering issues)
from app.core.config import settings  # noqa: E402
from app.core.logging import logger  # noqa: E402
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402


def _describe(msg):
    if isinstance(msg, dict):
        role = msg.get("role") or msg.get("type")
        tc = msg.get("tool_calls")
        tcid = msg.get("tool_call_id")
        content = msg.get("content")
    else:
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        tc = getattr(msg, "tool_calls", None)
        tcid = getattr(msg, "tool_call_id", None)
        content = getattr(msg, "content", None)
    if isinstance(content, list):
        content = f"<blocks:{len(content)}>"
    elif isinstance(content, str):
        content = content[:70].replace("\n", " ")
    return role, tc, tcid, content


async def main():
    thread_id = os.getenv("THREAD_ID", "")
    db_url = (
        f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
    )
    async with AsyncPostgresSaver.from_conn_string(db_url) as cp:
        config = {"configurable": {"thread_id": thread_id}}
        tup = await cp.aget_tuple(config)
        if tup is None:
            print(f"NO CHECKPOINT for thread_id={thread_id}")
            return
        channel_values = tup.checkpoint.get("channel_values", {})
        msgs = channel_values.get("messages", [])
        print(f"thread_id={thread_id}  total_messages={len(msgs)}")
        print(f"status={channel_values.get('status')}  is_complete={channel_values.get('is_complete')}")

        if os.getenv("DUMP_STATE", "") == "1":
            print("=" * 100)
            for field in ("plan", "code_changes", "test_results", "feedback", "errors", "artifacts"):
                value = channel_values.get(field)
                print(f"{field}: {json.dumps(value, ensure_ascii=False, default=str)[:2000]}")
            print("=" * 100)

        print("-" * 100)
        for i, m in enumerate(msgs):
            role, tc, tcid, content = _describe(m)
            tc_info = ""
            if tc:
                ids = [c.get("id") if isinstance(c, dict) else getattr(c, "id", "?") for c in tc]
                tc_info = f" tool_calls={ids}"
            tcid_info = f" tool_call_id={tcid}" if tcid else ""
            print(f"[{i:3d}] role={role}{tcid_info}{tc_info}  :: {content}")
        print("-" * 100)
        # Now simulate the trim the supervisor applies.
        from app.utils import prepare_sdlc_messages

        prepared = prepare_sdlc_messages(msgs, "SYSTEM")
        print(f"after prepare_sdlc_messages: {len(prepared)} messages")
        for i, m in enumerate(prepared):
            role, tc, tcid, content = _describe(m)
            tc_info = " tool_calls" if tc else ""
            tcid_info = f" tool_call_id={tcid}" if tcid else ""
            print(f"[{i:3d}] role={role}{tcid_info}{tc_info}  :: {content}")
        print("-" * 100)
        # Validate the tool-call pairing rule the API enforces.
        problems = []
        pending = set()
        for i, m in enumerate(prepared):
            role, tc, tcid, _ = _describe(m)
            if role in ("ai", "assistant"):
                if tc:
                    for c in tc:
                        pending.add(c.get("id") if isinstance(c, dict) else getattr(c, "id", None))
                else:
                    pending.clear()
            elif role in ("tool",):
                if tcid not in pending:
                    problems.append(f"[{i}] tool message tool_call_id={tcid} has no preceding tool_calls")
                else:
                    pending.discard(tcid)
            elif role in ("human", "user", "system"):
                pending.clear()
        if problems:
            print("VALIDATION PROBLEMS:")
            for p in problems:
                print("  " + p)
        else:
            print("VALIDATION OK: all tool messages paired")
        _ = json.dumps({"ok": True})


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        logger.error("diag_failed", error=str(e))
        raise
