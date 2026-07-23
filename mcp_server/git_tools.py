"""Read-only git introspection tools for the MCP server.

All tools are strictly read-only (log/show/branch/status/diff) — no
commit/push/merge/add. This keeps the security narrative clean: the MCP
server can never mutate the workspace, only observe it.

Path safety: ``repo_path`` must resolve under ``MCP_WORKSPACE_ROOT``
(default ``/workspace``). This prevents the LLM from probing repos
outside the sandbox via path traversal.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKSPACE_ROOT = Path(os.getenv("MCP_WORKSPACE_ROOT", "/workspace"))


def _validate_repo_path(repo_path: str) -> Path:
    """Resolve and validate that ``repo_path`` is under the workspace root.

    Args:
        repo_path: Path to the git repo, relative to workspace root or absolute.

    Returns:
        The resolved absolute Path.

    Raises:
        ValueError: If the path escapes the workspace root or is not a git repo.
    """
    root = _WORKSPACE_ROOT.resolve()
    target = Path(repo_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"repo_path {repo_path!r} is outside workspace root {root}") from exc
    if not (target / ".git").exists():
        raise ValueError(f"{target} is not a git repository")
    return target


def _run_git(repo: Path, args: list[str], timeout: int = 10) -> str:
    """Run a git command in ``repo`` and return stdout.

    Args:
        repo: Absolute path to the git repository.
        args: Git subcommand args (e.g. ``["log", "-10"]``).
        timeout: Subprocess timeout in seconds.

    Returns:
        Stripped stdout.

    Raises:
        RuntimeError: If git exits non-zero.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def git_log(repo_path: str = ".", max_count: int = 10, oneline: bool = True) -> str:
    """Get the git commit log (read-only).

    Args:
        repo_path: Path to the git repo, relative to workspace root.
        max_count: Maximum number of commits to return.
        oneline: If True, use ``--oneline`` format for brevity.

    Returns:
        Git log output, or ``"No commits yet"`` if empty.
    """
    repo = _validate_repo_path(repo_path)
    args = ["log", f"-{max_count}"]
    if oneline:
        args.append("--oneline")
    return _run_git(repo, args) or "No commits yet"


def git_show(repo_path: str = ".", rev: str = "HEAD", max_lines: int = 200) -> str:
    """Show details of a specific commit (read-only).

    Args:
        repo_path: Path to the git repo, relative to workspace root.
        rev: Commit ref (hash, tag, or ``HEAD``).
        max_lines: Truncate output to this many lines.

    Returns:
        ``git show`` output, truncated to ``max_lines``.
    """
    repo = _validate_repo_path(repo_path)
    out = _run_git(repo, ["show", rev])
    lines = out.splitlines()
    if len(lines) > max_lines:
        out = "\n".join(lines[:max_lines])
        out += f"\n... ({len(lines) - max_lines} more lines truncated)"
    return out


def git_branch_list(repo_path: str = ".") -> str:
    """List all local branches with tracking info (read-only).

    Args:
        repo_path: Path to the git repo, relative to workspace root.

    Returns:
        ``git branch --list -vv`` output.
    """
    repo = _validate_repo_path(repo_path)
    return _run_git(repo, ["branch", "--list", "-vv"]) or "No branches"


def git_status_short(repo_path: str = ".") -> str:
    """Get short git status (read-only).

    Args:
        repo_path: Path to the git repo, relative to workspace root.

    Returns:
        ``git status --short`` output, or ``"Working tree clean"``.
    """
    repo = _validate_repo_path(repo_path)
    return _run_git(repo, ["status", "--short"]) or "Working tree clean"


def git_diff_stat(repo_path: str = ".", base: str = "HEAD") -> str:
    """Get diff stat against a base ref (read-only).

    Args:
        repo_path: Path to the git repo, relative to workspace root.
        base: Base ref to diff against (default ``HEAD``).

    Returns:
        ``git diff --stat`` output, or ``"No changes"``.
    """
    repo = _validate_repo_path(repo_path)
    return _run_git(repo, ["diff", "--stat", base]) or "No changes"
