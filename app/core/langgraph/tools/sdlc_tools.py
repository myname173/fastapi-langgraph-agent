"""SDLC tools for LangGraph agent workflows.

This module provides sandboxed tools for software development lifecycle operations:
- File operations (read, write, edit, list)
- Code search
- Test execution (Docker-sandboxed)
- Git operations
- Script execution (Docker-sandboxed)

All tools operate within a workspace/{thread_id} sandbox directory.
Code execution tools (run_tests, generate_and_run_script) run inside
Docker containers for isolation — see ``app.core.sandbox``.
"""

import re
import subprocess
from pathlib import Path
from typing import List, Optional

from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import sdlc_security_issues_total
from app.services.image_service import image_service


def get_workspace_path(thread_id: str, path: str) -> Path:
    """Get absolute path within the workspace sandbox.

    Args:
        thread_id: The thread ID for isolation
        path: Relative path to resolve

    Returns:
        Absolute path within the workspace

    Raises:
        ValueError: If path traversal is detected
    """
    workspace_root = settings.SDLC_WORKSPACE_ROOT / thread_id
    workspace_root.mkdir(parents=True, exist_ok=True)

    resolved_path = (workspace_root / path).resolve()
    workspace_root_abs = workspace_root.resolve()

    if not str(resolved_path).startswith(str(workspace_root_abs)):
        raise ValueError(f"Path traversal detected: {path}")

    return resolved_path


def is_allowed_extension(file_path: Path) -> bool:
    """Check if file extension is allowed.

    Args:
        file_path: Path to check

    Returns:
        True if extension is allowed, False otherwise
    """
    ext = file_path.suffix.lstrip(".")
    return ext in settings.SDLC_ALLOWED_EXTENSIONS


def validate_file_path(thread_id: str, path: str, write: bool = False) -> Path:
    """Validate file path for operations.

    Args:
        thread_id: The thread ID
        path: Relative path
        write: Whether this is a write operation

    Returns:
        Validated absolute path

    Raises:
        ValueError: If path is invalid
    """
    abs_path = get_workspace_path(thread_id, path)

    if write:
        if not is_allowed_extension(abs_path):
            raise ValueError(f"File extension not allowed: {abs_path.suffix}")

    if not abs_path.exists() and not write:
        raise ValueError(f"File not found: {path}")

    return abs_path


@tool
def read_file(thread_id: str, path: str) -> str:
    """Read the contents of a file.

    Args:
        thread_id: The thread ID for workspace isolation
        path: Relative path to the file within the workspace

    Returns:
        File content as string

    Raises:
        ValueError: If file not found or path is invalid
    """
    try:
        abs_path = validate_file_path(thread_id, path)

        if abs_path.stat().st_size > settings.SDLC_MAX_FILE_SIZE:
            raise ValueError(f"File size exceeds limit: {abs_path.stat().st_size} bytes")

        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()

        logger.info("sdlc_tool_read_file", thread_id=thread_id, path=path, size=len(content))
        return content
    except Exception as e:
        logger.error("sdlc_tool_read_file_error", thread_id=thread_id, path=path, error=str(e))
        raise


@tool
def write_file(thread_id: str, path: str, content: str) -> str:
    """Write content to a file.

    Args:
        thread_id: The thread ID for workspace isolation
        path: Relative path to the file within the workspace
        content: Content to write

    Returns:
        Success message

    Raises:
        ValueError: If path is invalid or file size exceeds limit
    """
    try:
        abs_path = validate_file_path(thread_id, path, write=True)

        if len(content.encode("utf-8")) > settings.SDLC_MAX_FILE_SIZE:
            raise ValueError(f"Content size exceeds limit: {len(content)} bytes")

        check_security(content)

        abs_path.parent.mkdir(parents=True, exist_ok=True)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info("sdlc_tool_write_file", thread_id=thread_id, path=path, size=len(content))
        return f"Successfully wrote {len(content)} bytes to {path}"
    except Exception as e:
        logger.error("sdlc_tool_write_file_error", thread_id=thread_id, path=path, error=str(e))
        raise


@tool
def edit_file(thread_id: str, path: str, old_str: str, new_str: str) -> str:
    """Edit a file by replacing old_str with new_str.

    Args:
        thread_id: The thread ID for workspace isolation
        path: Relative path to the file within the workspace
        old_str: String to replace
        new_str: Replacement string

    Returns:
        Success message with number of replacements

    Raises:
        ValueError: If file not found or old_str not present
    """
    try:
        abs_path = validate_file_path(thread_id, path)

        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()

        if old_str not in content:
            raise ValueError(f"Old string not found in file: {old_str[:50]}...")

        new_content = content.replace(old_str, new_str)

        check_security(new_content)

        replacements = content.count(old_str)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        logger.info(
            "sdlc_tool_edit_file",
            thread_id=thread_id,
            path=path,
            replacements=replacements,
            changed_bytes=len(new_str) - len(old_str),
        )
        return f"Successfully replaced {replacements} occurrence(s) in {path}"
    except Exception as e:
        logger.error("sdlc_tool_edit_file_error", thread_id=thread_id, path=path, error=str(e))
        raise


@tool
def list_directory(thread_id: str, path: str = ".") -> List[dict]:
    """List files and directories in the workspace.

    Args:
        thread_id: The thread ID for workspace isolation
        path: Relative path to the directory within the workspace

    Returns:
        List of dictionaries with file info (name, type, size)
    """
    try:
        abs_path = get_workspace_path(thread_id, path)

        if not abs_path.exists():
            return []

        items = []
        for entry in sorted(abs_path.iterdir()):
            items.append(
                {
                    "name": entry.name,
                    "type": "directory" if entry.is_dir() else "file",
                    "size": entry.stat().st_size if entry.is_file() else 0,
                }
            )

        logger.info("sdlc_tool_list_directory", thread_id=thread_id, path=path, count=len(items))
        return items
    except Exception as e:
        logger.error("sdlc_tool_list_directory_error", thread_id=thread_id, path=path, error=str(e))
        return []


@tool
def search_code(thread_id: str, query: str, file_pattern: str = "*.py") -> List[dict]:
    """Search for code patterns in files.

    Args:
        thread_id: The thread ID for workspace isolation
        query: Search query string or regex pattern
        file_pattern: File glob pattern to filter files

    Returns:
        List of matches with file path, line number, and context
    """
    try:
        workspace_path = get_workspace_path(thread_id, ".")

        matches = []
        for file_path in workspace_path.rglob(file_pattern):
            if not file_path.is_file():
                continue

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                for i, line in enumerate(lines, 1):
                    if re.search(query, line, re.IGNORECASE):
                        matches.append(
                            {
                                "file": str(file_path.relative_to(workspace_path)),
                                "line": i,
                                "context": line.strip(),
                            }
                        )
            except Exception:
                continue

        logger.info("sdlc_tool_search_code", thread_id=thread_id, query=query, matches=len(matches))
        return matches[:50]
    except Exception as e:
        logger.error("sdlc_tool_search_code_error", thread_id=thread_id, query=query, error=str(e))
        return []


ALLOWED_TEST_COMMANDS = ["pytest", "python", "python3"]
DANGEROUS_CHARS = r'[;&|`$\'"<>]'


SENSITIVE_PATTERNS = [
    {
        "pattern": r'(?i)(api[_-]?key|api[_-]?secret|secret[_-]?key)\s*[=:]\s*["\'][\w\-]{16,}["\']',
        "severity": "critical",
        "message": "Hardcoded API key/secret detected",
    },
    {
        "pattern": r'(?i)password\s*[=:]\s*["\'][\w\-]{8,}["\']',
        "severity": "high",
        "message": "Hardcoded password detected",
    },
    {
        "pattern": r'(?i)private[_-]?(key|pem|cert)\s*[=:]\s*["\'][a-zA-Z0-9+/=\\-\\s]{40,}["\']',
        "severity": "critical",
        "message": "Hardcoded private key detected",
    },
    {
        "pattern": r'(?i)(access[_-]?token|auth[_-]?token|bearer[_-]?token)\s*[=:]\s*["\'][\w\-_.~+/]{20,}["\']',
        "severity": "high",
        "message": "Hardcoded token detected",
    },
    {
        "pattern": r"(?i)(postgres|mysql|mongodb|redis):\/\/[\w:-]+@",
        "severity": "high",
        "message": "Hardcoded database connection with credentials",
    },
    {
        "pattern": r"(?i)(?<!\w)([A-Z0-9]{32,48})(?!\w)",
        "severity": "medium",
        "message": "Potential hardcoded credential (long hex string)",
    },
    {
        "pattern": r'(?i)(select\s+.*from\s+.*where\s+.*=.*["\'].*["\'])',
        "severity": "medium",
        "message": "Potential SQL injection pattern",
    },
    {
        "pattern": r"(?i)(document\.write\(|innerHTML\s*=)",
        "severity": "medium",
        "message": "Potential XSS vulnerability pattern",
    },
]


def scan_security_issues(content: str) -> list[dict]:
    """Scan content for security issues.

    Args:
        content: Content to scan

    Returns:
        List of security issues found
    """
    issues = []
    for pattern_def in SENSITIVE_PATTERNS:
        matches = list(re.finditer(pattern_def["pattern"], content))
        for match in matches[:5]:
            context = content[max(0, match.start() - 30) : min(len(content), match.end() + 30)]
            issues.append(
                {
                    "severity": pattern_def["severity"],
                    "message": pattern_def["message"],
                    "match": match.group(0)[:100],
                    "context": context.strip(),
                }
            )
    return issues


def check_security(content: str) -> None:
    """Check content for security issues and raise if critical found.

    Args:
        content: Content to check

    Raises:
        ValueError: If critical security issues are found
    """
    if not settings.ENABLE_SECURITY_SCAN:
        return

    issues = scan_security_issues(content)
    if issues:
        for issue in issues:
            sdlc_security_issues_total.labels(severity=issue["severity"], agent_name="security_scan").inc()

        critical_issues = [i for i in issues if i["severity"] == "critical"]
        if critical_issues:
            issue_list = "\n".join([f"- {i['message']}: {i['match']}" for i in critical_issues])
            raise ValueError(f"Critical security issues found:\n{issue_list}")


@tool
def security_scan(thread_id: str, path: str = "") -> list[dict]:
    """Scan files for security issues.

    Args:
        thread_id: The thread ID for workspace isolation
        path: Optional path to scan (defaults to all files)

    Returns:
        List of security issues found
    """
    if not settings.ENABLE_SECURITY_SCAN:
        return [{"message": "Security scan is disabled", "severity": "info"}]

    try:
        workspace_path = get_workspace_path(thread_id, ".")

        all_issues = []
        files_to_scan = []

        if path:
            abs_path = get_workspace_path(thread_id, path)
            if abs_path.is_file():
                files_to_scan.append(abs_path)
            elif abs_path.is_dir():
                files_to_scan = list(abs_path.rglob("*.py"))
        else:
            files_to_scan = list(workspace_path.rglob("*.py"))

        for file_path in files_to_scan[:20]:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                issues = scan_security_issues(content)
                for issue in issues:
                    issue["file"] = str(file_path.relative_to(workspace_path))
                    all_issues.append(issue)
            except Exception:
                continue

        logger.info("sdlc_tool_security_scan", thread_id=thread_id, issues_found=len(all_issues))
        return all_issues[:50]
    except Exception as e:
        logger.error("sdlc_tool_security_scan_error", thread_id=thread_id, error=str(e))
        return [{"message": f"Scan failed: {str(e)}", "severity": "error"}]


def sanitize_command(command: str) -> list[str]:
    """Sanitize command to prevent injection attacks.

    Args:
        command: Command string to sanitize

    Returns:
        List of command arguments

    Raises:
        ValueError: If command contains dangerous characters
    """
    if re.search(DANGEROUS_CHARS, command):
        raise ValueError(f"Command contains dangerous characters: {command}")

    parts = command.split()
    if not parts:
        raise ValueError("Empty command")

    if parts[0] not in ALLOWED_TEST_COMMANDS:
        raise ValueError(f"Command not allowed: {parts[0]}. Allowed: {ALLOWED_TEST_COMMANDS}")

    return parts


async def _run_tests_subprocess_fallback(
    cmd_args: list[str],
    cwd: Path,
    timeout: float = 120.0,
) -> str:
    """Execute tests via restricted subprocess when Docker sandbox is unavailable.

    Security restrictions:
    - Command already validated by sanitize_command (pytest/python/python3 only)
    - Working directory restricted to workspace path
    - Sensitive environment variables stripped by pattern match
    - Execution timeout enforced

    Args:
        cmd_args: Validated command and arguments.
        cwd: Workspace working directory (already validated within workspace).
        timeout: Maximum execution seconds.

    Returns:
        Formatted output string matching run_tests format.
    """
    import asyncio
    import os
    import sys

    # Strip sensitive env vars by pattern (not hardcoded names).
    sensitive_patterns = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASS", "CREDENTIAL")
    sanitized_env = {k: v for k, v in os.environ.items() if not any(p in k.upper() for p in sensitive_patterns)}
    # Preserve system vars needed for command execution.
    for required in ("PATH", "PYTHONPATH", "LANG", "HOME"):
        if required in os.environ:
            sanitized_env[required] = os.environ[required]

    # Prepend the venv bin directory (derived from sys.executable) to PATH so
    # that pytest/python/python3 resolve to the project venv instead of the
    # system interpreter. The app process is launched via the venv's uvicorn,
    # but os.environ["PATH"] does not include the venv bin dir — without this,
    # subprocess_exec("pytest", ...) raises FileNotFoundError.
    venv_bin = os.path.dirname(sys.executable)
    sanitized_env["PATH"] = venv_bin + os.pathsep + sanitized_env.get("PATH", "")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            cwd=str(cwd),
            env=sanitized_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return_code = proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stdout, stderr = b"", f"Process timed out after {timeout}s".encode()
            return_code = -1
    except Exception as e:
        return f"STDOUT:\n\nSTDERR:\nSubprocess failed: {e}\nReturn code: -1\nSandbox result: fallback_error"

    return (
        f"STDOUT:\n{stdout.decode('utf-8', errors='replace')}\n\n"
        f"STDERR:\n{stderr.decode('utf-8', errors='replace')}\n\n"
        f"Return code: {return_code}\n"
        f"Sandbox result: fallback_subprocess"
    )


@tool
async def run_tests(thread_id: str, test_command: str = "pytest -v", path: str = ".") -> str:
    """Run tests in the workspace inside a Docker sandbox.

    Args:
        thread_id: The thread ID for workspace isolation
        test_command: Test command to execute (default: pytest -v)
        path: Relative path to run tests from

    Returns:
        Test output as string

    Raises:
        ValueError: If command execution fails or sandbox is unavailable
    """
    from app.core.sandbox import sandbox as docker_sandbox

    try:
        abs_path = get_workspace_path(thread_id, path)

        if not abs_path.exists():
            raise ValueError(f"Path not found: {path}")

        cmd_args = sanitize_command(test_command)

        # Determine the container working directory relative to /workspace.
        rel_path = path if path not in (".", "./", "") else "."
        working_dir = f"/workspace/{rel_path}" if rel_path not in (".", "./", "") else "/workspace"

        if settings.SANDBOX_BACKEND != "docker":
            raise ValueError(
                "Code execution requires SANDBOX_BACKEND=docker. "
                f"Current backend: {settings.SANDBOX_BACKEND!r}. "
                "Set SANDBOX_BACKEND=docker and ensure Docker is running."
            )

        result = await docker_sandbox.run(
            cmd=cmd_args,
            workspace=abs_path.parent if rel_path not in (".", "./", "") else abs_path,
            timeout=300,
            memory=settings.SANDBOX_MEMORY_LIMIT,
            cpus=settings.SANDBOX_CPU_LIMIT,
            working_dir=working_dir,
        )

        if result.result.value == "disabled":
            # Docker sandbox unavailable — fallback to restricted subprocess.
            # Maintains security via: command whitelist (sanitize_command),
            # workspace cwd isolation, sensitive env var stripping, timeout.
            # NOT equivalent to SANDBOX_BACKEND=host (forbidden by config.py)
            # — it's a degraded mode of the docker backend with extra limits.
            logger.warning(
                "sdlc_tool_run_tests_fallback_subprocess",
                thread_id=thread_id,
                command=test_command,
            )
            return await _run_tests_subprocess_fallback(
                cmd_args=cmd_args,
                cwd=abs_path,
            )

        output = (
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}\n\n"
            f"Return code: {result.return_code}\n"
            f"Sandbox result: {result.result.value}"
        )

        logger.info(
            "sdlc_tool_run_tests",
            thread_id=thread_id,
            command=test_command,
            path=path,
            return_code=result.return_code,
            sandbox_result=result.result.value,
        )
        return output
    except ValueError:
        raise
    except Exception as e:
        logger.error("sdlc_tool_run_tests_error", thread_id=thread_id, command=test_command, error=str(e))
        raise


@tool
def git_status(thread_id: str) -> str:
    """Get git status for the workspace.

    Args:
        thread_id: The thread ID for workspace isolation

    Returns:
        Git status output

    Raises:
        ValueError: If git is not enabled or not a git repository
    """
    if not settings.SDLC_GIT_ENABLED:
        raise ValueError("Git operations are disabled")

    try:
        abs_path = get_workspace_path(thread_id, ".")

        result = subprocess.run(
            ["git", "status"],
            cwd=abs_path,
            shell=False,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return "Not a git repository or git command failed"

        logger.info("sdlc_tool_git_status", thread_id=thread_id)
        return result.stdout
    except Exception as e:
        logger.error("sdlc_tool_git_status_error", thread_id=thread_id, error=str(e))
        raise


@tool
def git_diff(thread_id: str, path: Optional[str] = None) -> str:
    """Get git diff for the workspace.

    Args:
        thread_id: The thread ID for workspace isolation
        path: Optional path to diff (defaults to all changes)

    Returns:
        Git diff output

    Raises:
        ValueError: If git is not enabled or command fails
    """
    if not settings.SDLC_GIT_ENABLED:
        raise ValueError("Git operations are disabled")

    try:
        abs_path = get_workspace_path(thread_id, ".")

        if path is not None:
            if re.search(DANGEROUS_CHARS, path):
                raise ValueError(f"Path contains dangerous characters: {path}")
            cmd_args = ["git", "diff", path]
        else:
            cmd_args = ["git", "diff"]

        result = subprocess.run(
            cmd_args,
            cwd=abs_path,
            shell=False,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return f"Git diff failed: {result.stderr}"

        logger.info("sdlc_tool_git_diff", thread_id=thread_id, path=path or "all")
        return result.stdout or "No changes to diff"
    except Exception as e:
        logger.error("sdlc_tool_git_diff_error", thread_id=thread_id, error=str(e))
        raise


@tool
def apply_multi_file_edits(thread_id: str, edits: List[dict]) -> dict:
    """Apply multiple file edits in a single batch with rollback support.

    All edits are applied transactionally: if any edit fails, all changes
    are rolled back from backups. Each edit is security-scanned before
    being committed.

    Args:
        thread_id: The thread ID for workspace isolation
        edits: List of edit dicts, each with:
            - file_path: target file relative path
            - edit_type: "replace", "insert", "create", "delete"
            - old_str: string to replace (required for "replace")
            - new_str: replacement/new content
            - reason: optional explanation

    Returns:
        Dict with success status, applied count, failed edit info,
        and diff summary. On failure, includes rollback status.
    """
    if not settings.ENABLE_SMART_EDITING:
        raise ValueError("Smart editing is disabled. Use individual edit_file instead.")

    if len(edits) > settings.SMART_EDIT_MAX_FILES:
        raise ValueError(f"Too many edits: {len(edits)} > {settings.SMART_EDIT_MAX_FILES}")

    backups: dict[str, str] = {}
    applied: list[str] = []
    results: list[dict] = []

    def _rollback() -> None:
        for file_path, content in backups.items():
            try:
                abs_path = validate_file_path(thread_id, file_path, write=True)
                abs_path.write_text(content, encoding="utf-8")
            except Exception as rb_err:
                logger.error(
                    "sdlc_multi_edit_rollback_failed",
                    thread_id=thread_id,
                    file=file_path,
                    error=str(rb_err),
                )

    try:
        for i, edit in enumerate(edits):
            file_path = edit.get("file_path", "")
            edit_type = edit.get("edit_type", "replace")
            old_str = edit.get("old_str")
            new_str = edit.get("new_str", "")
            reason = edit.get("reason", "")

            if not file_path or not new_str:
                raise ValueError(f"Edit {i}: missing file_path or new_str")

            abs_path = validate_file_path(thread_id, file_path, write=True)

            if edit_type == "create":
                if abs_path.exists():
                    raise ValueError(f"Edit {i}: file already exists: {file_path}")
                check_security(new_str)
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(new_str, encoding="utf-8")
                applied.append(file_path)
                results.append({"file": file_path, "status": "created"})
                continue

            if not abs_path.exists():
                raise ValueError(f"Edit {i}: file not found: {file_path}")

            original = abs_path.read_text(encoding="utf-8")

            if file_path not in backups:
                backups[file_path] = original

            if edit_type == "replace":
                if not old_str:
                    raise ValueError(f"Edit {i}: old_str required for replace")
                if old_str not in original:
                    raise ValueError(f"Edit {i}: old_str not found in {file_path}")
                new_content = original.replace(old_str, new_str, 1)
            elif edit_type == "insert":
                if not old_str:
                    new_content = new_str + "\n" + original
                else:
                    if old_str not in original:
                        raise ValueError(f"Edit {i}: old_str not found in {file_path}")
                    new_content = original.replace(old_str, old_str + "\n" + new_str, 1)
            elif edit_type == "delete":
                if not old_str:
                    raise ValueError(f"Edit {i}: old_str required for delete")
                if old_str not in original:
                    raise ValueError(f"Edit {i}: old_str not found in {file_path}")
                new_content = original.replace(old_str, "", 1)
            else:
                raise ValueError(f"Edit {i}: unknown edit_type: {edit_type}")

            check_security(new_content)
            abs_path.write_text(new_content, encoding="utf-8")

            if file_path not in applied:
                applied.append(file_path)

            results.append(
                {
                    "file": file_path,
                    "status": "modified",
                    "edit_type": edit_type,
                    "reason": reason,
                }
            )

        logger.info(
            "sdlc_multi_edit_success",
            thread_id=thread_id,
            edit_count=len(edits),
            files_affected=len(set(applied)),
        )

        return {
            "success": True,
            "applied_count": len(results),
            "files_affected": len(set(applied)),
            "results": results,
            "rolled_back": False,
        }

    except Exception as e:
        logger.error(
            "sdlc_multi_edit_failed",
            thread_id=thread_id,
            error=str(e),
            applied_count=len(applied),
        )
        _rollback()
        return {
            "success": False,
            "error": str(e),
            "applied_count": len(applied),
            "results": results,
            "rolled_back": True,
        }


@tool
def validate_changes(thread_id: str, file_paths: Optional[List[str]] = None) -> dict:
    """Validate code changes with linting and security scanning.

    Runs ruff check on modified files and performs security scanning.
    Designed for post-edit validation to catch issues before they propagate.

    Args:
        thread_id: The thread ID for workspace isolation
        file_paths: Optional list of specific files to validate.
            If None, validates all Python files in workspace.

    Returns:
        Dict with validation results:
            - passed: bool (overall pass/fail)
            - lint_errors: list of lint issues
            - security_issues: list of security issues
            - warnings: list of warnings
            - message: summary
    """
    if not settings.ENABLE_POST_EDIT_VALIDATION:
        return {
            "passed": True,
            "lint_errors": [],
            "security_issues": [],
            "warnings": [],
            "message": "Post-edit validation is disabled",
        }

    workspace = get_workspace_path(thread_id, ".")
    lint_errors: list[dict] = []
    security_issues: list[dict] = []
    warnings: list[dict] = []

    try:
        if file_paths:
            target_files = [str(get_workspace_path(thread_id, fp)) for fp in file_paths if fp.endswith(".py")]
        else:
            target_files = [str(p) for p in workspace.rglob("*.py")]

        if not target_files:
            return {
                "passed": True,
                "lint_errors": [],
                "security_issues": [],
                "warnings": [],
                "message": "No Python files to validate",
            }

        try:
            result = subprocess.run(
                ["uv", "run", "ruff", "check", *target_files],
                cwd=workspace,
                shell=False,
                capture_output=True,
                text=True,
                timeout=settings.VALIDATION_TIMEOUT_SECONDS,
            )

            if result.returncode != 0 and result.stdout:
                for line in result.stdout.strip().split("\n")[:20]:
                    parts = line.split(":")
                    if len(parts) >= 4:
                        lint_errors.append(
                            {
                                "file": parts[0],
                                "line": parts[1],
                                "column": parts[2],
                                "code": parts[3].strip().split()[0],
                                "message": " ".join(parts[3].strip().split()[1:]),
                            }
                        )
                    else:
                        warnings.append({"type": "lint", "message": line})
        except subprocess.TimeoutExpired:
            warnings.append({"type": "timeout", "message": "Ruff check timed out"})
        except Exception as lint_err:
            warnings.append({"type": "lint_error", "message": str(lint_err)})

        for file_path in target_files[:20]:
            try:
                abs_path = Path(file_path)
                if abs_path.exists():
                    content = abs_path.read_text(encoding="utf-8")
                    issues = scan_security_issues(content)
                    for issue in issues:
                        security_issues.append(
                            {
                                **issue,
                                "file": str(abs_path.relative_to(workspace)),
                            }
                        )
            except Exception:
                continue

        passed = (
            len(lint_errors) == 0 and len([i for i in security_issues if i["severity"] in ["high", "critical"]]) == 0
        )

        message = f"Validation {'passed' if passed else 'failed'}"
        if lint_errors:
            message += f" ({len(lint_errors)} lint errors)"
        if security_issues:
            critical_count = len([i for i in security_issues if i["severity"] in ["high", "critical"]])
            message += f" ({critical_count} critical security issues)"

        logger.info(
            "sdlc_validate_changes",
            thread_id=thread_id,
            passed=passed,
            lint_errors=len(lint_errors),
            security_issues=len(security_issues),
        )

        return {
            "passed": passed,
            "lint_errors": lint_errors,
            "security_issues": security_issues,
            "warnings": warnings,
            "message": message,
        }

    except Exception as e:
        logger.error("sdlc_validate_changes_failed", thread_id=thread_id, error=str(e))
        return {
            "passed": False,
            "lint_errors": [],
            "security_issues": [],
            "warnings": [{"type": "validation_error", "message": str(e)}],
            "message": f"Validation failed: {str(e)}",
        }


sdlc_tools_list: list = []


@tool
async def generate_and_run_script(
    thread_id: str,
    script_content: str,
    script_name: str = "generated_script.py",
    language: str = "python",
) -> dict:
    """Generate and execute a script inside a Docker sandbox.

    The script is saved to ``workspace/{thread_id}/scripts/`` on the host
    (read-only inside the container) and executed in a Docker container
    with no network and dropped capabilities. Output files written to
    ``/outputs`` inside the container are visible in the captured stdout
    but **not** persisted back to the host workspace — long-running
    output collection is intentionally out of scope here for security.

    Args:
        thread_id: The thread ID for workspace isolation
        script_content: The script source code to execute
        script_name: Name of the script file (default: generated_script.py)
        language: Script language (only "python" is supported)

    Returns:
        Dict with execution results:
        - script_path: Path to the saved script (relative to workspace)
        - stdout: Standard output
        - stderr: Standard error
        - return_code: Exit code (0 = success)
        - success: Whether execution succeeded
        - timeout: Whether execution timed out
        - sandbox_result: Outcome category (success/failed/timeout/disabled)
        - error: Error message if any
    """
    from app.core.sandbox import sandbox as docker_sandbox

    if not settings.ENABLE_SCRIPT_EXECUTION:
        return {
            "script_path": "",
            "stdout": "",
            "stderr": "Script execution is disabled.",
            "return_code": -1,
            "success": False,
            "timeout": False,
            "sandbox_result": "disabled",
            "error": "Script execution is disabled by feature flag",
        }

    if language != "python":
        return {
            "script_path": "",
            "stdout": "",
            "stderr": f"Unsupported language: {language}. Only 'python' is supported.",
            "return_code": -1,
            "success": False,
            "timeout": False,
            "sandbox_result": "disabled",
            "error": f"Unsupported language: {language}",
        }

    if settings.SANDBOX_BACKEND != "docker":
        return {
            "script_path": "",
            "stdout": "",
            "stderr": (f"Code execution requires SANDBOX_BACKEND=docker (current: {settings.SANDBOX_BACKEND!r})."),
            "return_code": -1,
            "success": False,
            "timeout": False,
            "sandbox_result": "disabled",
            "error": "sandbox_backend_not_docker",
        }

    try:
        if not script_name.endswith(".py"):
            script_name = script_name + ".py"

        workspace_root = get_workspace_path(thread_id, ".")
        script_dir = workspace_root / "scripts"
        script_dir.mkdir(parents=True, exist_ok=True)

        check_security(script_content)
        script_path = script_dir / script_name
        script_path.write_text(script_content, encoding="utf-8")

        result = await docker_sandbox.run(
            cmd=["python", f"/workspace/scripts/{script_name}"],
            workspace=workspace_root,
            timeout=settings.SCRIPT_TIMEOUT_SECONDS,
            memory=settings.SANDBOX_MEMORY_LIMIT,
            cpus=settings.SANDBOX_CPU_LIMIT,
            working_dir="/outputs",
        )

        success = result.result.value == "success"
        logger.info(
            "sdlc_tool_generate_and_run_script",
            thread_id=thread_id,
            script_name=script_name,
            return_code=result.return_code,
            success=success,
            sandbox_result=result.result.value,
        )

        return {
            "script_path": f"scripts/{script_name}",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.return_code,
            "success": success,
            "timeout": result.result.value == "timeout",
            "sandbox_result": result.result.value,
            "error": None if success else (result.error or result.stderr),
        }

    except Exception as e:
        logger.error("sdlc_tool_script_error", thread_id=thread_id, script_name=script_name, error=str(e))
        return {
            "script_path": f"scripts/{script_name}",
            "stdout": "",
            "stderr": str(e),
            "return_code": -1,
            "success": False,
            "timeout": False,
            "sandbox_result": "failed",
            "error": str(e),
        }


@tool
def save_output_file(
    thread_id: str,
    content: str,
    filename: str,
    subdirectory: str = "",
) -> dict:
    """Save output content to a file in the workspace outputs directory.

    Args:
        thread_id: The thread ID for workspace isolation
        content: Content to write to the file
        filename: Name of the output file
        subdirectory: Optional subdirectory within outputs/ (e.g., "images")

    Returns:
        Dict with:
            - file_path: Relative path to the saved file
            - success: Whether the save succeeded
            - error: Error message if any
    """
    try:
        relative_path = f"outputs/{subdirectory}/{filename}" if subdirectory else f"outputs/{filename}"
        file_path = get_workspace_path(thread_id, relative_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        file_path.write_text(content, encoding="utf-8")

        logger.info("sdlc_tool_save_output_file", thread_id=thread_id, filename=filename)
        return {
            "file_path": relative_path,
            "success": True,
            "error": None,
        }
    except Exception as e:
        logger.error("sdlc_tool_save_output_error", thread_id=thread_id, filename=filename, error=str(e))
        return {
            "file_path": "",
            "success": False,
            "error": str(e),
        }


@tool
async def analyze_image(
    thread_id: str,
    image_id_or_url: str,
    context_query: str = "",
) -> dict:
    """Analyze an image using Vision LLM.

    Args:
        thread_id: The thread ID for workspace isolation
        image_id_or_url: Either an image ID (from upload) or a URL
        context_query: Optional context to guide the analysis

    Returns:
        Dict with analysis results:
            - image_id: Unique identifier
            - description: Detailed description of the image
            - key_elements: List of key elements identified
            - suggestions: List of suggestions based on the image
    """
    if not settings.ENABLE_MULTIMODAL:
        return {
            "image_id": image_id_or_url,
            "description": "Multimodal support is disabled.",
            "key_elements": [],
            "suggestions": [],
        }

    try:
        result = await image_service.analyze_image(image_id_or_url, thread_id, context_query)
        logger.info("sdlc_tool_analyze_image", thread_id=thread_id, image_id=result.image_id)
        return result.model_dump()
    except Exception as e:
        logger.error("sdlc_tool_analyze_image_error", thread_id=thread_id, error=str(e))
        return {
            "image_id": image_id_or_url,
            "description": f"Image analysis failed: {str(e)}",
            "key_elements": [],
            "suggestions": [],
        }


sdlc_tools_list = [
    read_file,
    write_file,
    edit_file,
    list_directory,
    search_code,
    run_tests,
    git_status,
    git_diff,
    security_scan,
    apply_multi_file_edits,
    validate_changes,
    analyze_image,
    generate_and_run_script,
    save_output_file,
]
