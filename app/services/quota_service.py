"""Resource quota service for SDLC workspace management.

This module provides:
- Workspace size quota checking
- Per-task image count limits
- Per-user task count limits
- Integration with Prometheus metrics
"""

import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator, Optional

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.db.session import get_async_session_maker
from app.core.db_executor import run_in_thread
from app.core.logging import logger
from app.core.metrics import (
    sdlc_quota_rejections_total,
    sdlc_workspace_size_bytes,
)
from app.models.sdlc_task import SDLCTask, SDLCTaskStatus
from app.services.database import database_service


class QuotaService:
    """Service for checking and enforcing resource quotas."""

    def __init__(self) -> None:
        """Initialize per-user locks for quota enforcement."""
        # M-15 fix: per-user asyncio.Lock dict to serialize the
        # check-then-create sequence in reserve_task_slot(). Without this,
        # two concurrent create_task calls for the same user can both pass
        # the count check and both insert a row, exceeding
        # MAX_TASKS_PER_USER (TOCTOU race).
        #
        # _lock_guard protects the dict mutation itself (the dict is
        # accessed from both the event loop and, via run_in_thread, from
        # worker threads — plain dict assignment is not atomic across
        # threads in all CPython implementations).
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._lock_guard = threading.Lock()
        # M-12 fix: per-user asyncio.Semaphore for video generation
        # concurrency. Video polling blocks 1-5 min; a per-user cap
        # prevents one user from tying up many event-loop slots.
        self._video_semaphores: dict[int, asyncio.Semaphore] = {}
        # M-13 fix: per-user daily media generation counter. Maps user_id
        # to (date_string, count). In-memory only — resets on restart,
        # which is acceptable for best-effort free-quota protection.
        self._media_counts: dict[int, tuple[str, int]] = {}
        self._vision_counts: dict[int, tuple[str, int]] = {}

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        """Get or create the per-user asyncio.Lock.

        Args:
            user_id: User ID to lock on.

        Returns:
            The asyncio.Lock for this user. The lock is bound to the
            running event loop the first time it is awaited, so callers
            must always invoke this from the same loop (the async API
            path).
        """
        with self._lock_guard:
            if user_id not in self._user_locks:
                self._user_locks[user_id] = asyncio.Lock()
            return self._user_locks[user_id]

    def _get_workspace_dir(self, thread_id: str) -> Path:
        """Get the workspace directory for a thread.

        Args:
            thread_id: Thread ID for isolation

        Returns:
            Path to the workspace directory
        """
        return settings.SDLC_WORKSPACE_ROOT / thread_id

    def _get_directory_size(self, path: Path) -> int:
        """Calculate total size of a directory in bytes.

        Args:
            path: Directory path

        Returns:
            Total size in bytes
        """
        if not path.exists():
            return 0

        total = 0
        try:
            for entry in path.rglob("*"):
                if entry.is_file():
                    total += entry.stat().st_size
        except (PermissionError, OSError) as e:
            logger.warning("directory_size_check_failed", path=str(path), error=str(e))
        return total

    def check_workspace_quota(self, thread_id: str) -> bool:
        """Check if workspace is within size quota.

        Args:
            thread_id: Thread ID for workspace isolation

        Returns:
            True if within quota, False if exceeded
        """
        if not settings.ENABLE_RESOURCE_QUOTA:
            return True

        workspace_dir = self._get_workspace_dir(thread_id)
        size_bytes = self._get_directory_size(workspace_dir)
        max_bytes = settings.MAX_WORKSPACE_SIZE_MB * 1024 * 1024

        sdlc_workspace_size_bytes.labels(thread_id=thread_id).set(size_bytes)

        if size_bytes > max_bytes:
            logger.warning(
                "workspace_quota_exceeded",
                thread_id=thread_id,
                size_mb=round(size_bytes / (1024 * 1024), 2),
                max_mb=settings.MAX_WORKSPACE_SIZE_MB,
            )
            sdlc_quota_rejections_total.labels(quota_type="workspace").inc()
            return False

        if size_bytes > max_bytes * 0.8:
            logger.warning(
                "workspace_quota_warning",
                thread_id=thread_id,
                size_mb=round(size_bytes / (1024 * 1024), 2),
                max_mb=settings.MAX_WORKSPACE_SIZE_MB,
                usage_pct=round(size_bytes / max_bytes * 100, 1),
            )

        return True

    def check_image_count_quota(self, thread_id: str) -> bool:
        """Check if task has exceeded image count limit.

        Args:
            thread_id: Thread ID for workspace isolation

        Returns:
            True if within quota, False if exceeded
        """
        if not settings.ENABLE_RESOURCE_QUOTA:
            return True

        image_dir = self._get_workspace_dir(thread_id) / "images"
        if not image_dir.exists():
            return True

        image_count = sum(
            1 for f in image_dir.iterdir() if f.is_file() and f.suffix[1:].lower() in settings.ALLOWED_IMAGE_TYPES
        )

        if image_count >= settings.MAX_IMAGES_PER_TASK:
            logger.warning(
                "image_count_quota_exceeded",
                thread_id=thread_id,
                current_count=image_count,
                max_count=settings.MAX_IMAGES_PER_TASK,
            )
            sdlc_quota_rejections_total.labels(quota_type="images").inc()
            return False

        return True

    # ------------------------------------------------------------------
    # L-11 fix: async wrappers for the file-system-traversing quota checks.
    # check_workspace_quota and check_image_count_quota use path.rglob("*")
    # and iterdir() which are blocking calls. When invoked from an async
    # context (e.g. image_service.save_uploaded_image) they stall the event
    # loop for the duration of the traversal. These wrappers offload the
    # blocking work to a thread via run_in_thread. The original sync methods
    # are kept for direct use in tests.
    # ------------------------------------------------------------------

    async def check_workspace_quota_async(self, thread_id: str) -> bool:
        """Async wrapper — runs the blocking traversal off-loop (L-11)."""
        return await run_in_thread(self.check_workspace_quota, thread_id, op="check_workspace_quota")

    async def check_image_count_quota_async(self, thread_id: str) -> bool:
        """Async wrapper — runs the blocking traversal off-loop (L-11)."""
        return await run_in_thread(self.check_image_count_quota, thread_id, op="check_image_count_quota")

    def _count_active_tasks_sync(self, user_id: int) -> int:
        def _block(session: Session) -> int:
            # L-10 fix: use SQL COUNT(*) instead of len(list(result.all())).
            # The old form materialized every matching row into memory just
            # to count them; COUNT(*) runs entirely server-side.
            result = session.execute(
                select(func.count())
                .select_from(SDLCTask)
                .where(
                    SDLCTask.user_id == user_id,
                    col(SDLCTask.status).not_in([SDLCTaskStatus.COMPLETED, SDLCTaskStatus.CANCELLED]),
                )
            )
            return int(result.scalar_one())

        return database_service.run_in_session(_block, op="count_active_tasks")

    async def _count_active_tasks_async(self, user_id: int) -> int:
        """Native async version of :meth:`_count_active_tasks_sync`."""
        # L-10 fix: use SQL COUNT(*) instead of len(list(result.scalars().all())).
        async with get_async_session_maker()() as session:
            result = await session.execute(
                select(func.count())
                .select_from(SDLCTask)
                .where(
                    SDLCTask.user_id == user_id,
                    col(SDLCTask.status).not_in([SDLCTaskStatus.COMPLETED, SDLCTaskStatus.CANCELLED]),
                )
            )
            return int(result.scalar_one())

    async def _count_active_tasks(self, user_id: int) -> int:
        """Count active tasks via the configured DB path (sync or async)."""
        if settings.ENABLE_ASYNC_DB:
            return await self._count_active_tasks_async(user_id)
        return await run_in_thread(self._count_active_tasks_sync, user_id, op="count_active_tasks")

    @asynccontextmanager
    async def reserve_task_slot(self, user_id: Optional[int]) -> AsyncIterator[None]:
        """Reserve a task slot for a user, enforcing MAX_TASKS_PER_USER.

        This is the TOCTOU-safe replacement for the check-then-create
        pattern (M-15 fix). The per-user asyncio.Lock is held across both
        the count check and the caller's task creation, so two concurrent
        ``create_task`` calls for the same user cannot both pass the
        quota check.

        Usage::

            async with quota_service.reserve_task_slot(user_id):
                task = await sdlc_task_service._create_task_async(...)

        Args:
            user_id: User ID to reserve for. ``None`` is a no-op pass-
                through (quota enforcement requires a real user ID).

        Yields:
            None — the caller performs the task creation inside the
            ``async with`` block.

        Raises:
            ValueError: If the user has reached MAX_TASKS_PER_USER active
                tasks. Raised at entry (before yield), so the caller's
                creation code never runs when over quota.
        """
        if not settings.ENABLE_RESOURCE_QUOTA or user_id is None:
            # No enforcement — nothing to lock around.
            yield
            return

        lock = self._get_user_lock(user_id)
        async with lock:
            active_count = await self._count_active_tasks(user_id)
            if active_count >= settings.MAX_TASKS_PER_USER:
                logger.warning(
                    "task_count_quota_exceeded",
                    user_id=user_id,
                    active_count=active_count,
                    max_count=settings.MAX_TASKS_PER_USER,
                )
                sdlc_quota_rejections_total.labels(quota_type="tasks").inc()
                raise ValueError(
                    f"User {user_id} has reached the maximum of {settings.MAX_TASKS_PER_USER} active tasks"
                )
            # Lock is still held here — the caller's create runs inside
            # the `async with` block, so no concurrent request for the
            # same user can squeeze in between check and create.
            yield

    async def check_task_count_quota(self, user_id: Optional[int]) -> bool:
        """Check if user has exceeded task count limit.

        .. deprecated::
            This method is retained for backward compatibility and ad-hoc
            pre-checks. It is **not** TOCTOU-safe — between this check
            returning ``True`` and the caller creating a task, a
            concurrent request may also pass and exceed the limit.
            Production callers that create tasks should use
            :meth:`reserve_task_slot` instead, which holds a per-user lock
            across the check-then-create sequence (M-15 fix).

        Args:
            user_id: User ID to check

        Returns:
            True if within quota, False if exceeded
        """
        if not settings.ENABLE_RESOURCE_QUOTA or user_id is None:
            return True

        active_count = await self._count_active_tasks(user_id)

        if active_count >= settings.MAX_TASKS_PER_USER:
            logger.warning(
                "task_count_quota_exceeded",
                user_id=user_id,
                active_count=active_count,
                max_count=settings.MAX_TASKS_PER_USER,
            )
            sdlc_quota_rejections_total.labels(quota_type="tasks").inc()
            return False

        return True

    # ------------------------------------------------------------------
    # Media generation quotas (M-12 + M-13)
    # ------------------------------------------------------------------

    def _get_video_semaphore(self, user_id: int) -> asyncio.Semaphore:
        """Get or create the per-user video generation semaphore."""
        with self._lock_guard:
            if user_id not in self._video_semaphores:
                self._video_semaphores[user_id] = asyncio.Semaphore(settings.MAX_CONCURRENT_VIDEO_PER_USER)
            return self._video_semaphores[user_id]

    @asynccontextmanager
    async def acquire_video_slot(self, user_id: Optional[int]) -> AsyncIterator[None]:
        """Limit concurrent video generations per user (M-12 fix).

        Video polling blocks the event loop for 1-5 minutes per request.
        Without a per-user cap, a single user could fire several concurrent
        video requests and tie up event-loop capacity. This semaphore
        serializes video generations per user (default 1 at a time).

        Usage::

            async with quota_service.acquire_video_slot(session.user_id):
                result = await media_service.generate_video(...)
        """
        if user_id is None:
            yield
            return
        sem = self._get_video_semaphore(user_id)
        async with sem:
            yield

    def check_media_quota(self, user_id: Optional[int]) -> bool:
        """Check if user can generate more media today (M-13 fix).

        Uses an in-memory per-user daily counter that resets at UTC
        midnight. Best-effort: resets on process restart. The goal is to
        prevent a single user from exhausting the shared account-level
        DashScope free quota, not to provide precise billing.

        Args:
            user_id: User ID to check.

        Returns:
            True if the user is under the daily media cap, False if
            exceeded.
        """
        if not settings.ENABLE_RESOURCE_QUOTA or user_id is None:
            return True

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        with self._lock_guard:
            date_str, count = self._media_counts.get(user_id, (today, 0))
            if date_str != today:
                # New UTC day — reset the counter.
                self._media_counts[user_id] = (today, 0)
                count = 0
            if count >= settings.MAX_MEDIA_GENERATIONS_PER_USER_PER_DAY:
                logger.warning(
                    "media_quota_exceeded",
                    user_id=user_id,
                    daily_count=count,
                    max_daily=settings.MAX_MEDIA_GENERATIONS_PER_USER_PER_DAY,
                )
                sdlc_quota_rejections_total.labels(quota_type="media").inc()
                return False
            return True

    def increment_media_usage(self, user_id: Optional[int]) -> None:
        """Increment the user's daily media counter (M-13 fix).

        Call after a successful media generation (image or video) so the
        next check_media_quota reflects the updated usage.
        """
        if user_id is None:
            return
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        with self._lock_guard:
            date_str, count = self._media_counts.get(user_id, (today, 0))
            if date_str != today:
                count = 0
            self._media_counts[user_id] = (today, count + 1)

    def check_vision_quota(self, user_id: Optional[int]) -> bool:
        """Check if user can run more chat vision analyses today.

        Independent of the media-generation quota. Uses the same in-memory
        per-user UTC-daily counter pattern. Gated by ``ENABLE_RESOURCE_QUOTA``;
        anonymous users always pass.
        """
        if not settings.ENABLE_RESOURCE_QUOTA or user_id is None:
            return True
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        with self._lock_guard:
            date_str, count = self._vision_counts.get(user_id, (today, 0))
            if date_str != today:
                self._vision_counts[user_id] = (today, 0)
                count = 0
            if count >= settings.MAX_VISION_ANALYSES_PER_USER_PER_DAY:
                logger.warning(
                    "vision_quota_exceeded",
                    user_id=user_id,
                    daily_count=count,
                    max_daily=settings.MAX_VISION_ANALYSES_PER_USER_PER_DAY,
                )
                sdlc_quota_rejections_total.labels(quota_type="vision").inc()
                return False
            return True

    def increment_vision_usage(self, user_id: Optional[int]) -> None:
        """Increment the user's daily chat vision-analysis counter.

        Call after a successful vision analysis so the next check_vision_quota
        reflects updated usage.
        """
        if user_id is None:
            return
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        with self._lock_guard:
            date_str, count = self._vision_counts.get(user_id, (today, 0))
            if date_str != today:
                count = 0
            self._vision_counts[user_id] = (today, count + 1)


quota_service = QuotaService()
