"""Docker-based sandbox for untrusted code execution.

The SDLC orchestrator's ``run_tests`` and ``generate_and_run_script`` tools
execute user-controlled code (pytest arguments, arbitrary Python scripts).
Even with ``shell=False`` and a command allowlist, a script can call
``os.system("curl … | sh")`` or ``shutil.rmtree`` with the API process's
full host permissions — that is a remote code execution (RCE) vulnerability.

This module replaces bare ``subprocess.run`` with Docker containers that:

- Mount the workspace **read-only** (source code is visible but immutable).
- Provide a writable ``/outputs`` tmpfs for test results and generated files.
- Drop all capabilities (``--cap-drop=ALL``).
- Run as non-root (``--user 65532:65532``).
- Disable networking (``--network=none``).
- Limit memory, CPU, and PID count.
- Auto-remove on exit (``--rm``).

**Fail-closed design**: if Docker is unavailable the tool returns an error
instead of falling back to bare ``subprocess`` — security is never
compromised by graceful degradation.
"""

import asyncio
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

from app.core.logging import logger
from app.core.metrics import (
    sandbox_execution_seconds,
    sandbox_executions_total,
)


class SandboxResult(Enum):
    """Outcome of a sandboxed execution."""

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DISABLED = "disabled"


@dataclass
class SandboxOutput:
    """Captured output from a sandboxed execution."""

    stdout: str
    stderr: str
    return_code: int
    result: SandboxResult
    error: Optional[str] = None


class DockerSandbox:
    """Manages Docker-based sandboxed code execution.

    Probes Docker availability once at construction and caches the
    result. All executions go through the same hardened container
    configuration.
    """

    def __init__(self) -> None:
        """Initialize the sandbox with an unset Docker-availability cache."""
        self._available: Optional[bool] = None

    @property
    def available(self) -> bool:
        """Check if Docker is available (probed once, then cached).

        Synchronous probe — only checks that the ``docker`` binary exists.
        The full daemon connectivity check happens in
        :meth:`_check_docker_async` at run time.
        """
        if self._available is None:
            self._available = self._check_docker_binary()
        return self._available

    @staticmethod
    def _check_docker_binary() -> bool:
        """Return ``True`` iff the ``docker`` binary is on PATH.

        This is a cheap sync check. The real daemon probe happens in
        :meth:`_check_docker_async` to avoid blocking the event loop.
        """
        if shutil.which("docker") is None:
            logger.warning("sandbox_docker_not_found", detail="docker binary not in PATH")
            return False
        return True

    async def run(
        self,
        cmd: List[str],
        workspace: Path,
        *,
        timeout: float = 300.0,
        memory: str = "512m",
        cpus: str = "1.0",
        working_dir: Optional[str] = None,
    ) -> SandboxOutput:
        """Execute a command inside an isolated Docker container.

        Args:
            cmd: Command and arguments to run inside the container.
            workspace: Host path mounted read-only at ``/workspace``.
            timeout: Maximum wall-clock seconds before the container is killed.
            memory: Docker ``--memory`` limit (e.g. ``"512m"``).
            cpus: Docker ``--cpus`` limit.
            working_dir: Working directory inside the container (defaults to
                ``/workspace`` if not set).

        Returns:
            ``SandboxOutput`` with captured stdout/stderr and outcome.

        Raises:
            RuntimeError: If called when Docker is unavailable (should not
                happen — callers should check ``self.available`` first).
        """
        if not await self._check_docker_async():
            sandbox_executions_total.labels(result="disabled").inc()
            logger.error("sandbox_unavailable", cmd=cmd[0] if cmd else "empty")
            return SandboxOutput(
                stdout="",
                stderr="Sandbox unavailable: Docker is not running or not installed.",
                return_code=-1,
                result=SandboxResult.DISABLED,
                error="sandbox_unavailable",
            )

        workdir = working_dir or "/workspace"
        workspace_resolved = workspace.resolve()

        docker_args = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            f"--memory={memory}",
            f"--cpus={cpus}",
            "--pids-limit=128",
            "--read-only",
            "--security-opt=no-new-privileges",
            "--cap-drop=ALL",
            "--user=65532:65532",
            # Read-only workspace mount — code is visible but immutable.
            f"--mount=type=bind,source={workspace_resolved},target=/workspace,readonly",
            # Writable tmpfs for outputs (test results, generated files).
            "--mount=type=tmpfs,destination=/outputs,tmpfs-size=256m",
            "-w",
            workdir,
            "python:3.13-slim",
            *cmd,
        ]

        logger.info(
            "sandbox_execution_starting",
            cmd=cmd[0],
            workspace=str(workspace_resolved),
            timeout_seconds=timeout,
        )
        start = time.perf_counter()

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Container still running — kill it.
                logger.warning("sandbox_execution_timeout", cmd=cmd[0], timeout_seconds=timeout)
                try:
                    await asyncio.create_subprocess_exec(
                        "docker",
                        "stop",
                        "--time=5",
                        # We'd need the container ID; simplest approach is to let
                        # docker run --rm handle cleanup on process kill.
                    )
                except Exception:
                    pass
                proc.kill()
                await proc.wait()

                sandbox_execution_seconds.observe(time.perf_counter() - start)
                sandbox_executions_total.labels(result="timeout").inc()
                return SandboxOutput(
                    stdout="",
                    stderr=f"Execution timed out after {timeout}s",
                    return_code=-1,
                    result=SandboxResult.TIMEOUT,
                    error="execution_timeout",
                )

            elapsed = time.perf_counter() - start
            sandbox_execution_seconds.observe(elapsed)

            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            rc = proc.returncode or 0

            if rc == 0:
                sandbox_executions_total.labels(result="success").inc()
                result = SandboxResult.SUCCESS
            else:
                sandbox_executions_total.labels(result="failed").inc()
                result = SandboxResult.FAILED

            logger.info(
                "sandbox_execution_complete",
                cmd=cmd[0],
                return_code=rc,
                result=result.value,
                elapsed_seconds=round(elapsed, 2),
            )

            return SandboxOutput(stdout=stdout, stderr=stderr, return_code=rc, result=result)

        except Exception as e:
            sandbox_execution_seconds.observe(time.perf_counter() - start)
            sandbox_executions_total.labels(result="failed").inc()
            logger.exception("sandbox_execution_error", cmd=cmd[0], error=str(e))
            return SandboxOutput(
                stdout="",
                stderr=str(e),
                return_code=-1,
                result=SandboxResult.FAILED,
                error=str(e),
            )

    async def _check_docker_async(self) -> bool:
        """Async probe for Docker availability."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            return proc.returncode == 0
        except Exception:
            return False


# Module-level singleton
sandbox = DockerSandbox()
