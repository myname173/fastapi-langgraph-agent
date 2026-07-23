"""Read-only sqlite query tools for the MCP server.

Queries a seed database (``mcp.db``) that mirrors the app's
``sdlc_task`` schema and holds eval golden sets for trajectory
assertions. All tools are read-only — the connection is opened with
``mode=ro`` at the sqlite level, so writes are physically impossible.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(os.getenv("MCP_DB_PATH", "/data/mcp.db"))


def _connect() -> sqlite3.Connection:
    """Open a read-only connection to the MCP sqlite database.

    Returns:
        A sqlite3 Connection with ``row_factory`` set to ``Row``.

    Raises:
        FileNotFoundError: If the database file does not exist.
    """
    if not _DB_PATH.exists():
        raise FileNotFoundError(f"MCP database not found at {_DB_PATH}. Run seed_db.py first.")
    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def query_tasks(status: str | None = None, limit: int = 20) -> str:
    """Query SDLC task history (read-only).

    Args:
        status: Filter by status (planning/coding/testing/reviewing/
            completed/cancelled/paused). ``None`` = all statuses.
        limit: Max rows to return.

    Returns:
        JSON array of task records (without the ``plan`` field for brevity).
    """
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT id, thread_id, user_id, status, requirements, "
                "created_at, updated_at, completed_at "
                "FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, thread_id, user_id, status, requirements, "
                "created_at, updated_at, completed_at "
                "FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2) or "[]"


def query_task_by_id(task_id: str) -> str:
    """Get a single task by ID, including its plan (read-only).

    Args:
        task_id: The task UUID.

    Returns:
        JSON object of the task, or ``"Task <id> not found"``.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, thread_id, user_id, status, requirements, plan, "
            "created_at, updated_at, completed_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        return f"Task {task_id} not found"
    return json.dumps(dict(row), ensure_ascii=False, indent=2)


def query_task_stats() -> str:
    """Get task counts grouped by status (read-only).

    Returns:
        JSON object mapping status -> count.
    """
    with _connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status ORDER BY cnt DESC").fetchall()
    return json.dumps({r["status"]: r["cnt"] for r in rows}, ensure_ascii=False, indent=2) or "{}"


def query_eval_golden(task_type: str | None = None) -> str:
    """Query eval golden sets for trajectory assertions (read-only).

    Args:
        task_type: Filter by task type. ``None`` = all.

    Returns:
        JSON array of golden records.
    """
    with _connect() as conn:
        if task_type:
            rows = conn.execute(
                "SELECT id, task_type, requirement, expected_files, expected_tools, "
                "expected_trajectory, max_steps, max_tokens "
                "FROM eval_golden WHERE task_type = ?",
                (task_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, task_type, requirement, expected_files, expected_tools, "
                "expected_trajectory, max_steps, max_tokens "
                "FROM eval_golden"
            ).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2) or "[]"
