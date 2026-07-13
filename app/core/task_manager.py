"""Supervised background task management.

Replaces scattered ``asyncio.create_task(...)`` calls that:

- are silently dropped on process shutdown (memory writes lost),
- swallow exceptions (the task object is never awaited, so errors vanish),
- have no visibility into pending vs. failed work.

Usage:

    from app.core.task_manager import task_manager

    # fire-and-forget, but tracked and logged
    await task_manager.schedule(memory_service.add(user_id, msgs), name="memory_add")

On shutdown, call ``await task_manager.shutdown()`` (wired in ``main.py``
lifespan) to cancel in-flight tasks and wait for them to unwind cleanly.
"""

import asyncio
from typing import Any, Awaitable

from app.core.logging import logger
from app.core.metrics import background_task_failures_total


class BackgroundTaskManager:
    """Tracks fire-and-forget coroutines so they are observable and cleanable."""

    def __init__(self) -> None:
        """Initialize an empty tracked-task set."""
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def pending_count(self) -> int:
        """Number of tasks currently tracked (not yet done)."""
        return len(self._tasks)

    async def schedule(self, coro: Awaitable[Any], *, name: str) -> asyncio.Task[Any]:
        """Schedule a coroutine as a tracked background task.

        Wraps ``asyncio.create_task`` with:

        - **Strong reference tracking**: the task is held in a set so the
          garbage collector cannot reap it mid-flight (a documented
          ``asyncio`` footgun).
        - **Structured failure logging**: exceptions are recorded via
          ``logger.exception`` and counted in
          ``background_task_failures_total{name}``.
        - **Cancellation safety**: ``CancelledError`` is treated as normal
          (shutdown) and not counted as a failure.

        Args:
            coro: The coroutine to run.
            name: Human-readable name (also the metric label) describing
                what the task does, e.g. ``"memory_add"``.

        Returns:
            The created ``asyncio.Task``. Callers usually ignore it
            (fire-and-forget), but tests may await it.
        """
        task = asyncio.create_task(self._run_safely(coro, name), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run_safely(self, coro: Awaitable[Any], name: str) -> Any:
        try:
            return await coro
        except asyncio.CancelledError:
            # Expected during shutdown; not a failure.
            logger.info("background_task_cancelled", task_name=name)
            raise
        except Exception:
            background_task_failures_total.labels(task_name=name).inc()
            logger.exception("background_task_failed", task_name=name)

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Cancel all tracked tasks and wait for them to unwind.

        Called during application shutdown (``main.py`` lifespan) so
        in-flight background work (e.g. memory writes) is either
        completed quickly or cancelled rather than abruptly killed.

        Args:
            timeout: Grace period (seconds) for tasks to observe
                cancellation. After this, remaining tasks are abandoned
                (they will be reaped when the interpreter exits).
        """
        if not self._tasks:
            return
        pending = list(self._tasks)
        cancelled_count = 0
        for task in pending:
            if not task.done():
                task.cancel()
                cancelled_count += 1
        logger.info(
            "background_task_shutdown_started",
            total=len(pending),
            cancelling=cancelled_count,
            timeout_seconds=timeout,
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "background_task_shutdown_timeout",
                still_running=len([t for t in self._tasks if not t.done()]),
                timeout_seconds=timeout,
            )
        self._tasks.clear()
        logger.info("background_task_shutdown_complete")


# Module-level singleton, imported throughout the app.
task_manager = BackgroundTaskManager()
