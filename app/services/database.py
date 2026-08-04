"""This file contains the database service for the application.

All public methods are ``async def``. When ``settings.ENABLE_ASYNC_DB``
is true, queries run on a **native async** SQLAlchemy 2.x
``AsyncSession`` (psycopg3 driver) — no thread dispatch, no GIL
contention. When the flag is false (default, legacy mode), queries
run on **synchronous** SQLModel sessions dispatched to a worker thread
via :func:`app.core.db_executor.run_in_thread`. This keeps the event
loop free under load while preserving the original behaviour.

Query logic is identical between the two paths; only the dispatch
mechanism differs. The async path will become the default once the
migration is validated end-to-end.
"""

import warnings
from typing import (
    Callable,
    List,
    Optional,
    TypeVar,
)

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool
from sqlmodel import (
    Session,
    col,
    create_engine,
    select,
)

from app.core.config import (
    Environment,
    settings,
)
from app.core.db.session import get_async_session_maker
from app.core.db_executor import run_in_thread
from app.core.logging import logger
from app.models.session import Session as ChatSession
from app.models.user import User

R = TypeVar("R")


class DatabaseService:
    """Service class for database operations.

    Exposes async methods backed by a synchronous SQLModel engine. Every
    public method dispatches its query to a worker thread so the event
    loop is never blocked.
    """

    def __init__(self):
        """Initialize database service with connection pool."""
        try:
            # Configure environment-specific database connection pool settings
            pool_size = settings.POSTGRES_POOL_SIZE
            max_overflow = settings.POSTGRES_MAX_OVERFLOW

            # Create engine with appropriate pool configuration
            connection_url = (
                f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
                f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
            )

            self.engine = create_engine(
                connection_url,
                pool_pre_ping=True,
                poolclass=QueuePool,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=30,  # Connection timeout (seconds)
                pool_recycle=1800,  # Recycle connections after 30 minutes
            )

            logger.info(
                "database_initialized",
                environment=settings.ENVIRONMENT.value,
                pool_size=pool_size,
                max_overflow=max_overflow,
            )
        except SQLAlchemyError as e:
            logger.error("database_initialization_error", error=str(e), environment=settings.ENVIRONMENT.value)
            # In production, don't raise - allow app to start even with DB issues
            if settings.ENVIRONMENT != Environment.PRODUCTION:
                raise

    # ------------------------------------------------------------------
    # Internal helpers for off-loop execution
    # ------------------------------------------------------------------

    def run_in_session(self, sync_work: Callable[[Session], R], *, op: str = "session_op") -> R:
        """Run a sync session-scoped block **inline** (synchronously).

        Despite the name, this is a *synchronous* helper intended to be
        called from inside :func:`run_in_thread` (or directly from a
        worker thread). It opens a session, runs ``sync_work``, and
        returns its result. Callers from async code must wrap the call:

            result = await run_in_thread(
                lambda: database_service.run_in_session(_my_block, op="x"),
                op="x",
            )

        Args:
            sync_work: Callable receiving an open ``Session`` and returning
                whatever the caller wants back.
            op: Logical op name used for the metric label.

        Returns:
            The value returned by ``sync_work``.
        """
        with Session(self.engine) as session:
            return sync_work(session)

    # ------------------------------------------------------------------
    # Sync query implementations (private, identical logic to before)
    # ------------------------------------------------------------------

    def _create_user_sync(self, email: str, password: str, username: str | None = None) -> User:
        with Session(self.engine) as session:
            user = User(email=email, hashed_password=password, username=username)
            session.add(user)
            session.commit()
            session.refresh(user)
            logger.info("user_created", email=email)
            return user

    def _get_user_sync(self, user_id: int) -> Optional[User]:
        with Session(self.engine) as session:
            user = session.get(User, user_id)
            return user

    def _get_user_by_email_sync(self, email: str) -> Optional[User]:
        with Session(self.engine) as session:
            statement = select(User).where(User.email == email)
            user = session.exec(statement).first()
            return user

    def _delete_user_by_email_sync(self, email: str) -> bool:
        with Session(self.engine) as session:
            user = session.exec(select(User).where(User.email == email)).first()
            if not user:
                return False

            session.delete(user)
            session.commit()
            logger.info("user_deleted", email=email)
            return True

    def _create_session_sync(
        self, session_id: str, user_id: int, name: str = "", username: str | None = None
    ) -> ChatSession:
        with Session(self.engine) as session:
            chat_session = ChatSession(id=session_id, user_id=user_id, name=name, username=username)
            session.add(chat_session)
            session.commit()
            session.refresh(chat_session)
            logger.info("session_created", session_id=session_id, user_id=user_id, name=name)
            return chat_session

    def _delete_session_sync(self, session_id: str) -> bool:
        with Session(self.engine) as session:
            chat_session = session.get(ChatSession, session_id)
            if not chat_session:
                return False

            session.delete(chat_session)
            session.commit()
            logger.info("session_deleted", session_id=session_id)
            return True

    def _get_session_sync(self, session_id: str) -> Optional[ChatSession]:
        with Session(self.engine) as session:
            chat_session = session.get(ChatSession, session_id)
            return chat_session

    def _get_user_sessions_sync(self, user_id: int) -> List[ChatSession]:
        with Session(self.engine) as session:
            statement = (
                select(ChatSession)
                .where(col(ChatSession.user_id) == user_id)
                .order_by(col(ChatSession.created_at).desc())
            )
            sessions = session.exec(statement).all()
            return list(sessions)

    def _update_session_name_sync(self, session_id: str, name: str) -> ChatSession:
        with Session(self.engine) as session:
            chat_session = session.get(ChatSession, session_id)
            if not chat_session:
                raise HTTPException(status_code=404, detail="Session not found")

            chat_session.name = name
            session.add(chat_session)
            session.commit()
            session.refresh(chat_session)
            logger.info("session_name_updated", session_id=session_id, name=name)
            return chat_session

    def _health_check_sync(self) -> bool:
        try:
            with Session(self.engine) as session:
                # Execute a simple query to check connection
                session.exec(select(1)).first()
                return True
        except Exception as e:
            logger.error("database_health_check_failed", error=str(e))
            return False

    # ------------------------------------------------------------------
    # Async query implementations (native AsyncSession, no thread dispatch)
    # ------------------------------------------------------------------
    # These mirror the ``_xxx_sync`` blocks above but run on the async
    # SQLAlchemy 2.x ``AsyncSession``. Activated per-request when
    # ``settings.ENABLE_ASYNC_DB`` is true. Query semantics are identical;
    # only the dispatch differs (no ``run_in_thread`` wrapper, no GIL).
    #
    # SQLModel models remain unchanged — only the session type differs.
    # Queries use SQLAlchemy 2.x ``session.execute(select(...))`` +
    # ``scalars()`` instead of SQLModel's ``session.exec(select(...))``
    # because the latter is sync-only.

    async def _create_user_async(self, email: str, password: str, username: str | None = None) -> User:
        async with get_async_session_maker()() as session:
            user = User(email=email, hashed_password=password, username=username)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info("user_created", email=email)
            return user

    async def _get_user_async(self, user_id: int) -> Optional[User]:
        async with get_async_session_maker()() as session:
            return await session.get(User, user_id)

    async def _get_user_by_email_async(self, email: str) -> Optional[User]:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(User).where(User.email == email))
            return result.scalars().first()

    async def _delete_user_by_email_async(self, email: str) -> bool:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(User).where(User.email == email))
            user = result.scalars().first()
            if not user:
                return False

            await session.delete(user)
            await session.commit()
            logger.info("user_deleted", email=email)
            return True

    async def _create_session_async(
        self, session_id: str, user_id: int, name: str = "", username: str | None = None
    ) -> ChatSession:
        async with get_async_session_maker()() as session:
            chat_session = ChatSession(id=session_id, user_id=user_id, name=name, username=username)
            session.add(chat_session)
            await session.commit()
            await session.refresh(chat_session)
            logger.info("session_created", session_id=session_id, user_id=user_id, name=name)
            return chat_session

    async def _delete_session_async(self, session_id: str) -> bool:
        async with get_async_session_maker()() as session:
            chat_session = await session.get(ChatSession, session_id)
            if not chat_session:
                return False

            await session.delete(chat_session)
            await session.commit()
            logger.info("session_deleted", session_id=session_id)
            return True

    async def _get_session_async(self, session_id: str) -> Optional[ChatSession]:
        async with get_async_session_maker()() as session:
            return await session.get(ChatSession, session_id)

    async def _get_user_sessions_async(self, user_id: int) -> List[ChatSession]:
        async with get_async_session_maker()() as session:
            statement = (
                select(ChatSession)
                .where(col(ChatSession.user_id) == user_id)
                .order_by(col(ChatSession.created_at).desc())
            )
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def _update_session_name_async(self, session_id: str, name: str) -> ChatSession:
        async with get_async_session_maker()() as session:
            chat_session = await session.get(ChatSession, session_id)
            if not chat_session:
                raise HTTPException(status_code=404, detail="Session not found")

            chat_session.name = name
            session.add(chat_session)
            await session.commit()
            await session.refresh(chat_session)
            logger.info("session_name_updated", session_id=session_id, name=name)
            return chat_session

    async def _health_check_async(self) -> bool:
        try:
            async with get_async_session_maker()() as session:
                await session.execute(select(1))
                return True
        except Exception as e:
            logger.error("database_health_check_failed", error=str(e))
            return False

    # ------------------------------------------------------------------
    # Public async API — routes to native async or sync+thread based on flag
    # ------------------------------------------------------------------

    async def create_user(self, email: str, password: str, username: str | None = None) -> User:
        """Create a new user.

        Args:
            email: User's email address
            password: Hashed password
            username: Optional display name

        Returns:
            User: The created user
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._create_user_async(email, password, username)
        return await run_in_thread(self._create_user_sync, email, password, username, op="create_user")

    async def get_user(self, user_id: int) -> Optional[User]:
        """Get a user by ID.

        Args:
            user_id: The ID of the user to retrieve

        Returns:
            Optional[User]: The user if found, None otherwise
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._get_user_async(user_id)
        return await run_in_thread(self._get_user_sync, user_id, op="get_user")

    async def get_user_by_email(self, email: str) -> Optional[User]:
        """Get a user by email.

        Args:
            email: The email of the user to retrieve

        Returns:
            Optional[User]: The user if found, None otherwise
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._get_user_by_email_async(email)
        return await run_in_thread(self._get_user_by_email_sync, email, op="get_user_by_email")

    async def delete_user_by_email(self, email: str) -> bool:
        """Delete a user by email.

        Args:
            email: The email of the user to delete

        Returns:
            bool: True if deletion was successful, False if user not found
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._delete_user_by_email_async(email)
        return await run_in_thread(self._delete_user_by_email_sync, email, op="delete_user_by_email")

    async def create_session(
        self, session_id: str, user_id: int, name: str = "", username: str | None = None
    ) -> ChatSession:
        """Create a new chat session.

        Args:
            session_id: The ID for the new session
            user_id: The ID of the user who owns the session
            name: Optional name for the session (defaults to empty string)
            username: Display name copied from the user for LLM personalization

        Returns:
            ChatSession: The created session
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._create_session_async(session_id, user_id, name, username)
        return await run_in_thread(self._create_session_sync, session_id, user_id, name, username, op="create_session")

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session by ID.

        Args:
            session_id: The ID of the session to delete

        Returns:
            bool: True if deletion was successful, False if session not found
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._delete_session_async(session_id)
        return await run_in_thread(self._delete_session_sync, session_id, op="delete_session")

    async def get_session(self, session_id: str) -> Optional[ChatSession]:
        """Get a session by ID.

        Args:
            session_id: The ID of the session to retrieve

        Returns:
            Optional[ChatSession]: The session if found, None otherwise
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._get_session_async(session_id)
        return await run_in_thread(self._get_session_sync, session_id, op="get_session")

    async def get_user_sessions(self, user_id: int) -> List[ChatSession]:
        """Get all sessions for a user.

        Args:
            user_id: The ID of the user

        Returns:
            List[ChatSession]: List of user's sessions
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._get_user_sessions_async(user_id)
        return await run_in_thread(self._get_user_sessions_sync, user_id, op="get_user_sessions")

    async def update_session_name(self, session_id: str, name: str) -> ChatSession:
        """Update a session's name.

        Args:
            session_id: The ID of the session to update
            name: The new name for the session

        Returns:
            ChatSession: The updated session

        Raises:
            HTTPException: If session is not found
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._update_session_name_async(session_id, name)
        return await run_in_thread(self._update_session_name_sync, session_id, name, op="update_session_name")

    def get_session_maker(self):
        """Return a session maker for creating database sessions.

        .. deprecated::
            Opening sessions inline via this maker inside ``async def``
            code blocks the event loop. New code should define a sync
            block and pass it to :meth:`run_in_session` (called from
            within :func:`run_in_thread`), or — when ``ENABLE_ASYNC_DB``
            is on — use :func:`app.core.db.session.get_async_session_maker`
            directly. Retained while services migrate.
        """
        warnings.warn(
            "get_session_maker() blocks the event loop when used inside async code; "
            "use DatabaseService.run_in_session() + run_in_thread() instead, "
            "or get_async_session_maker() when ENABLE_ASYNC_DB is on.",
            DeprecationWarning,
            stacklevel=2,
        )
        return Session(self.engine)

    async def health_check(self) -> bool:
        """Check database connection health.

        Returns:
            bool: True if database is healthy, False otherwise
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._health_check_async()
        return await run_in_thread(self._health_check_sync, op="health_check")


# Create a singleton instance
database_service = DatabaseService()
