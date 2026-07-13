"""Async SQLAlchemy 2.x session factory.

Builds the ``AsyncEngine`` and ``async_sessionmaker`` used by the
business DB layer when ``ENABLE_ASYNC_DB`` is enabled.

Driver choice
-------------

Uses ``postgresql+psycopg://`` (psycopg3 async) rather than
``postgresql+asyncpg://`` for three reasons:

1. **Dependency alignment** — ``psycopg[binary]`` is already locked in
   ``pyproject.toml``; ``asyncpg`` would add a new dependency.
2. **Pool sharing** — LangGraph's ``AsyncPostgresSaver`` uses the same
   psycopg driver (see ``app/core/db/pool.py``). Keeping the business
   DB on psycopg as well means one driver, one set of prepared-statement
   semantics, one observability surface.
3. **Feature parity** — psycopg3 pipeline mode matches asyncpg's
   performance within ~10% on typical workloads, and is more
   SQLAlchemy-friendly (the ``+psycopg`` dialect is fully supported).

Pool strategy
-------------

The async engine uses SQLAlchemy's default ``AsyncAdaptedQueuePool``
with the same ``pool_size`` / ``max_overflow`` as the legacy sync
engine. This is a **separate** pool from the ``AsyncConnectionPool``
in ``app/core/db/pool.py`` (which serves LangGraph). Unifying them
into a single shared pool is a future optimisation — for now, both
pools are async and the total connection count is still lower than
the legacy sync+async split.

Usage
-----

    from app.core.db.session import get_async_session

    @router.get("/items")
    async def list_items(session: AsyncSession = Depends(get_async_session)):
        result = await session.execute(select(Item))
        return result.scalars().all()

Or for service-layer code that wants a session without FastAPI:

    from app.core.db.session import async_session_maker

    async with async_session_maker() as session:
        ...
"""

from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.core.config import settings
from app.core.logging import logger
from urllib.parse import quote_plus

_engine: Optional[AsyncEngine] = None
_session_maker: Optional[async_sessionmaker[AsyncSession]] = None


def _build_async_url() -> str:
    """Build the async SQLAlchemy URL (``postgresql+psycopg://...``).

    Credentials are URL-quoted so special characters in the password do
    not corrupt the URL.
    """
    return (
        "postgresql+psycopg://"
        f"{quote_plus(settings.POSTGRES_USER)}:{quote_plus(settings.POSTGRES_PASSWORD)}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
    )


def init_async_engine() -> AsyncEngine:
    """Create the async engine and session maker.

    Idempotent: returns the existing engine if already initialised.

    The engine mirrors the legacy sync engine's pool configuration
    (``pool_size``, ``max_overflow``, ``pool_recycle``) so the
    PostgreSQL connection budget stays the same.

    Returns:
        The initialised ``AsyncEngine``.
    """
    global _engine, _session_maker
    if _engine is not None:
        return _engine

    _engine = create_async_engine(
        _build_async_url(),
        pool_pre_ping=True,
        poolclass=AsyncAdaptedQueuePool,
        pool_size=settings.POSTGRES_POOL_SIZE,
        max_overflow=settings.POSTGRES_MAX_OVERFLOW,
        pool_timeout=30,
        pool_recycle=1800,
        echo=False,
    )

    _session_maker = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,  # avoid lazy-load errors after commit
        autoflush=False,
    )

    logger.info(
        "async_engine_initialized",
        pool_size=settings.POSTGRES_POOL_SIZE,
        max_overflow=settings.POSTGRES_MAX_OVERFLOW,
        environment=settings.ENVIRONMENT.value,
    )
    return _engine


def get_async_session_maker() -> async_sessionmaker[AsyncSession]:
    """Return the session maker, initialising the engine on first call.

    Raises:
        RuntimeError: if called before ``init_async_engine`` has run
            and the engine failed to initialise.
    """
    if _session_maker is None:
        init_async_engine()
    assert _session_maker is not None  # narrowed by init_async_engine
    return _session_maker


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yield an ``AsyncSession`` and close on exit.

    Usage::

        @router.get("/")
        async def handler(session: AsyncSession = Depends(get_async_session)):
            ...

    The session is closed automatically when the request ends; callers
    still need to ``await session.commit()`` for writes.
    """
    maker = get_async_session_maker()
    async with maker() as session:
        yield session


async def close_async_engine() -> None:
    """Dispose the async engine. Idempotent.

    Called at app shutdown to release all pooled connections back to
    PostgreSQL.
    """
    global _engine, _session_maker
    if _engine is None:
        return

    try:
        await _engine.dispose()
        logger.info("async_engine_disposed")
    except Exception as e:
        logger.exception("async_engine_dispose_failed", error=str(e))
    finally:
        _engine = None
        _session_maker = None
