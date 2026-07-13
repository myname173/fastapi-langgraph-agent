"""Unified async PostgreSQL connection pool.

This module owns the ``psycopg.AsyncConnectionPool`` consumed by:

- **LangGraph checkpointer** — ``AsyncPostgresSaver`` (in
  ``app/core/langgraph/graph.py`` and
  ``app/core/langgraph/workflows/supervisor.py``) consumes raw
  ``AsyncConnection`` objects from this pool.
- **Semantic cache** — ``app/core/semantic_cache.py`` borrows
  connections for LLM response caching.

The business DB layer (``app/services/database.py`` +
``app/core/db/session.py``) uses **separate** SQLAlchemy engines:
``QueuePool`` (sync, when ``ENABLE_ASYNC_DB=false``, the current
default) or ``AsyncAdaptedQueuePool`` (async, when
``ENABLE_ASYNC_DB=true``). These engines cannot share this raw psycopg
pool because ``AsyncPostgresSaver`` requires a ``psycopg.AsyncConnectionPool``
while SQLAlchemy requires an ``AsyncEngine`` — the two APIs are
incompatible at the pool level. Unifying them is tracked as a future
optimisation (L-13, deferred as accepted technical debt — risk/reward
ratio is poor for a LOW-priority infrastructure refactor).

Centralising the LangGraph pool here (instead of leaving it inside
``LangGraphAgent``) has three benefits:

1. **Single lifecycle** — ``main.py`` lifespan opens/closes the pool
   exactly once; no scattered ``close()`` calls.
2. **Graceful degradation** — production keeps running (degraded mode)
   if the pool fails to initialise, mirroring the prior behaviour in
   ``graph.py``.
3. **Explicit ownership** — the pool is a process-wide singleton, not
   tied to any single agent instance.

Lifecycle contract
-------------------

1. ``await init_pool()`` — call once at app startup (idempotent).
2. ``get_pool()`` — sync accessor; returns ``None`` when the pool has
   not been initialised (degraded mode) so callers can fall back.
3. ``await close_pool()`` — call once at app shutdown (idempotent).
"""

from typing import Optional

from psycopg import (
    AsyncConnection,
)
from psycopg.rows import (
    DictRow,
    dict_row,
)
from psycopg_pool import AsyncConnectionPool
from urllib.parse import quote_plus

from app.core.config import (
    Environment,
    settings,
)
from app.core.logging import logger

PostgresConnPool = AsyncConnectionPool[AsyncConnection[DictRow]]

# Module-level singleton — initialised once at startup.
_pool: Optional[PostgresConnPool] = None


def _build_connection_url() -> str:
    """Build the PostgreSQL connection URL from settings.

    Credentials are URL-quoted so passwords containing special
    characters (``@``, ``:``, ``/``) do not corrupt the URL.
    """
    return (
        "postgresql://"
        f"{quote_plus(settings.POSTGRES_USER)}:{quote_plus(settings.POSTGRES_PASSWORD)}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
    )


async def init_pool() -> Optional[PostgresConnPool]:
    """Open the shared async connection pool.

    Idempotent: if the pool is already open, returns the existing
    instance without re-initialising.

    In production, pool initialisation failure is logged and ``None`` is
    returned so the app keeps running in a degraded mode (matching the
    prior ``graph.py`` behaviour). In non-production environments the
    exception is re-raised so tests/dev fail fast.

    Returns:
        The initialised pool, or ``None`` when production degrades.
    """
    global _pool
    if _pool is not None:
        return _pool

    max_size = settings.POSTGRES_POOL_SIZE
    try:
        # ``AsyncConnectionPool`` is generic over the connection type; the
        # ``row_factory=dict_row`` kwarg makes each row a ``DictRow`` but
        # pyright cannot infer that from kwargs, so we annotate explicitly.
        pool: PostgresConnPool = AsyncConnectionPool(
            _build_connection_url(),
            open=False,
            max_size=max_size,
            kwargs={
                "autocommit": True,
                "connect_timeout": 5,
                "prepare_threshold": None,
                "row_factory": dict_row,
            },
        )
        await pool.open()
        _pool = pool
        logger.info(
            "db_pool_initialized",
            max_size=max_size,
            environment=settings.ENVIRONMENT.value,
        )
        return _pool
    except Exception as e:
        logger.error(
            "db_pool_initialization_failed",
            error=str(e),
            environment=settings.ENVIRONMENT.value,
        )
        if settings.ENVIRONMENT == Environment.PRODUCTION:
            logger.warning(
                "db_pool_degraded_mode",
                environment=settings.ENVIRONMENT.value,
            )
            return None
        raise


def get_pool() -> Optional[PostgresConnPool]:
    """Return the shared pool, or ``None`` when not initialised.

    Sync accessor — call only after ``await init_pool()`` has run.
    Designed for callers (e.g. ``LangGraphAgent``) that already handle
    a ``None`` pool as degraded-mode.
    """
    return _pool


async def close_pool() -> None:
    """Close the shared pool if open. Idempotent.

    Logs the close event for observability. Swallows exceptions so
    shutdown never crashes the process.
    """
    global _pool
    if _pool is None:
        return

    try:
        await _pool.close()
        logger.info("db_pool_closed")
    except Exception as e:
        logger.exception("db_pool_close_failed", error=str(e))
    finally:
        _pool = None
