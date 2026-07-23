"""MCP server entrypoint — registers read-only git + sqlite tools and
serves them over the streamable-http transport.

The server is a CPU-only sidecar: zero GPU cost, zero node runtime. It
exposes git introspection (log/show/branch/status/diff) and read-only
sqlite queries over SDLC task history + eval golden sets, giving coder
and tester agents the ability to self-reflect on past work and git
context without any business-code changes in the main app.

Transport selection is configuration-driven via ``MCP_TRANSPORT``:

- ``streamable-http`` (default) — for production / docker-compose, the
  client connects to ``http://mcp:8002/mcp``.
- ``stdio`` — for local testing and unit tests (in-process, no port).

Run::

    python -m mcp_server.server                          # streamable-http
    MCP_TRANSPORT=stdio python -m mcp_server.server       # stdio (testing)
"""

from __future__ import annotations

import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mcp_server import git_tools, sqlite_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# DNS-rebinding protection root cause: FastMCP's __init__ auto-enables
# Host-header validation (enable_dns_rebinding_protection=True) with a
# localhost-only allowlist whenever the *ctor* `host` is 127.0.0.1/localhost/::1
# (the default). main() later mutates `mcp.settings.host = "0.0.0.0"`, but
# transport_security is already baked — so the app container's Host `mcp:8002`
# is rejected with HTTP 421 while host-side `localhost:8002` passes. Fix: pass
# an explicit transport_security whose allowed_hosts covers the compose service
# name `mcp`. `allowed_hosts` is a TransportSecuritySettings field (NOT a FastMCP
# ctor param — that crashes with TypeError); use `:*` wildcards since httpx sends
# Host with the port (`mcp:8002`).
mcp = FastMCP(
    "sdlc-context",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "mcp:*",
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "host.docker.internal:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    ),
)


# ---- git tools (read-only) ----------------------------------------------


@mcp.tool()
def git_log(repo_path: str = ".", max_count: int = 10, oneline: bool = True) -> str:
    """Get the git commit log for a repository (read-only).

    Use this to understand recent changes before modifying code.
    """
    return git_tools.git_log(repo_path, max_count, oneline)


@mcp.tool()
def git_show(repo_path: str = ".", rev: str = "HEAD", max_lines: int = 200) -> str:
    """Show details of a specific commit (read-only).

    Useful for inspecting what a previous change did.
    """
    return git_tools.git_show(repo_path, rev, max_lines)


@mcp.tool()
def git_branch_list(repo_path: str = ".") -> str:
    """List all local branches with tracking info (read-only)."""
    return git_tools.git_branch_list(repo_path)


@mcp.tool()
def git_status_short(repo_path: str = ".") -> str:
    """Get short git status (read-only).

    Shows uncommitted changes in the working tree.
    """
    return git_tools.git_status_short(repo_path)


@mcp.tool()
def git_diff_stat(repo_path: str = ".", base: str = "HEAD") -> str:
    """Get diff stat against a base ref (read-only)."""
    return git_tools.git_diff_stat(repo_path, base)


# ---- sqlite tools (read-only) -------------------------------------------


@mcp.tool()
def query_tasks(status: str | None = None, limit: int = 20) -> str:
    """Query SDLC task history (read-only).

    Returns a JSON array of past tasks. Filter by status for
    self-reflection on completed or cancelled work.
    """
    return sqlite_tools.query_tasks(status, limit)


@mcp.tool()
def query_task_by_id(task_id: str) -> str:
    """Get a single task by ID, including its plan (read-only)."""
    return sqlite_tools.query_task_by_id(task_id)


@mcp.tool()
def query_task_stats() -> str:
    """Get task counts grouped by status (read-only)."""
    return sqlite_tools.query_task_stats()


@mcp.tool()
def query_eval_golden(task_type: str | None = None) -> str:
    """Query eval golden sets for trajectory assertions (read-only)."""
    return sqlite_tools.query_eval_golden(task_type)


def main() -> None:
    """Run the MCP server with the configured transport.

    Note: ``mcp.run()`` in mcp 1.27 reads host/port from
    ``mcp.settings`` (not kwargs), so we mutate the settings object
    before starting. This keeps the configuration env-driven.
    """
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8002"))

    if transport == "stdio":
        logger.info("starting MCP server (stdio transport)")
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = host
        mcp.settings.port = port
        logger.info("starting MCP server (streamable-http) on %s:%d", host, port)
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
