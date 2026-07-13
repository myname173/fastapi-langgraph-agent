"""Async wrapper around synchronous DB operations.

The codebase uses SQLModel/SQLAlchemy **synchronous** sessions, but the
service layer is ``async def``. Calling the synchronous session directly
inside a coroutine blocks the entire event loop — under load this causes
P99 latency to cascade.

This module routes every blocking DB call through a thread pool
(``asyncio.to_thread``) so the event loop stays free. Query logic itself
is untouched (zero regression risk); only the dispatch moves off-loop.

Usage:

    from app.core.db_executor import run_in_thread

    # sync helper (private, identical to before)
    def _get_user_sync(user_id: int) -> User | None:
        with Session(engine) as session:
            return session.get(User, user_id)

    # async public method
    async def get_user(self, user_id: int) -> User | None:
        return await run_in_thread(self._get_user_sync, user_id, op="get_user")

For helpers that need a session they create themselves, prefer
``DatabaseService.run_in_session`` (see ``app/services/database.py``)
which opens and closes the session inside the worker thread.
"""

import asyncio
import time
from functools import partial
from typing import Any, Callable, TypeVar

from app.core.logging import logger
from app.core.metrics import db_operation_seconds

T = TypeVar("T")


async def run_in_thread(func: Callable[..., T], *args: Any, op: str = "db_op", **kwargs: Any) -> T:
    """Run a blocking callable in a worker thread without blocking the loop.

    Wraps ``asyncio.to_thread`` with:

    - **Duration metric**: recorded under ``db_operation_seconds{op}`` so
      regressions are visible in Prometheus/Grafana.
    - **Structured exception logging**: preserves the traceback via
      ``logger.exception`` (per project rule: never ``logger.error`` for
      exceptions).

    Args:
        func: Synchronous callable to execute off-loop.
        *args: Positional arguments forwarded to ``func``.
        op: Logical operation name used as the metric label.
        **kwargs: Keyword arguments forwarded to ``func``.

    Returns:
        Whatever ``func`` returns.

    Raises:
        Reraises any exception from ``func`` unchanged.
    """
    start = time.perf_counter()
    try:
        if kwargs:
            # asyncio.to_thread forwards only positional args, so bind kwargs
            # onto a partial and run the partial with no extra args.
            return await asyncio.to_thread(partial(func, *args, **kwargs))
        return await asyncio.to_thread(func, *args)
    except Exception:
        logger.exception("db_thread_op_failed", op=op)
        raise
    finally:
        db_operation_seconds.labels(op=op).observe(time.perf_counter() - start)
