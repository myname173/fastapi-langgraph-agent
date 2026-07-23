"""Seed the MCP sqlite database with demo data.

Creates ``mcp.db`` with two tables:

- ``tasks`` — mirrors the app's ``sdlc_task`` schema so the MCP server
  can expose SDLC task history to agents for self-reflection.
- ``eval_golden`` — expected files/tools per task type, used by the eval
  framework for trajectory assertions.

Idempotent: drops and recreates the database on each run.

Run::

    python -m mcp_server.seed_db
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, UTC
from pathlib import Path

_DB_PATH = Path(os.getenv("MCP_DB_PATH", "/data/mcp.db"))


def seed() -> None:
    """Create the MCP database and insert seed data."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _DB_PATH.exists():
        _DB_PATH.unlink()

    conn = sqlite3.connect(_DB_PATH)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id           TEXT PRIMARY KEY,
            thread_id    TEXT,
            user_id      INTEGER,
            status       TEXT,
            requirements TEXT,
            plan         TEXT,
            created_at   TEXT,
            updated_at   TEXT,
            completed_at TEXT
        );
        CREATE TABLE eval_golden (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type           TEXT,
            requirement         TEXT,
            expected_files      TEXT,
            expected_tools      TEXT,
            expected_trajectory TEXT,
            max_steps           INTEGER,
            max_tokens          INTEGER
        );
        """
    )

    now = datetime.now(UTC).isoformat()
    tasks = [
        (
            "t-001",
            "th-001",
            1,
            "completed",
            "Build a landing page with hero section and contact form",
            '[{"step":1,"action":"create index.html"},{"step":2,"action":"add styles.css"}]',
            now,
            now,
            now,
        ),
        (
            "t-002",
            "th-002",
            1,
            "completed",
            "Create a Python script to fetch stock prices",
            '[{"step":1,"action":"create fetch.py"}]',
            now,
            now,
            now,
        ),
        (
            "t-003",
            "th-003",
            1,
            "cancelled",
            "Build a dashboard (cancelled: scope too large)",
            "[]",
            now,
            now,
            None,
        ),
    ]
    conn.executemany(
        "INSERT INTO tasks (id, thread_id, user_id, status, requirements, "
        "plan, created_at, updated_at, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tasks,
    )

    # Golden dataset is sourced from evals/golden/tasks.py (single source of
    # truth) so the sqlite view never drifts from what the regression tests
    # assert against. If evals/ is unavailable (e.g. an MCP-only container
    # without the evals package), seed the tasks table only and leave
    # eval_golden empty — the MCP server still starts.
    try:
        _repo_root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(_repo_root))
        from evals.golden.tasks import GOLDEN_TASKS

        goldens = [
            (
                t.task_type,
                t.requirement,
                json.dumps(t.expected_files),
                json.dumps(t.expected_tools),
                json.dumps(t.expected_trajectory, ensure_ascii=False),
                t.max_steps,
                t.max_tokens,
            )
            for t in GOLDEN_TASKS
        ]
    except ImportError as exc:
        print(f"WARNING: evals.golden not available ({exc}); seeding tasks table only")
        goldens = []

    if goldens:
        conn.executemany(
            "INSERT INTO eval_golden (task_type, requirement, expected_files, "
            "expected_tools, expected_trajectory, max_steps, max_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            goldens,
        )

    conn.commit()
    conn.close()
    print(f"Seeded MCP database at {_DB_PATH}: {len(tasks)} tasks, {len(goldens)} goldens")


if __name__ == "__main__":
    seed()
