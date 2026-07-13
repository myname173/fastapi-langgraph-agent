"""Media History Service for recording and querying media generations.

Follows the same sync+async dual-path pattern as ``sdlc_task_service``:
when ``settings.ENABLE_ASYNC_DB`` is on, queries run on a native
AsyncSession; otherwise they are dispatched to a worker thread via
``run_in_thread``.
"""

import uuid
from typing import List, Optional

from sqlmodel import Session, desc, select

from app.core.config import settings
from app.core.db.session import get_async_session_maker
from app.core.db_executor import run_in_thread
from app.core.logging import logger
from app.models.media_history import MediaHistory, MediaStatus, MediaType
from app.services.database import database_service


class MediaHistoryService:
    """Service for recording and querying media generation history."""

    # ------------------------------------------------------------------
    # Sync DB blocks (private) — dispatched off-loop via run_in_thread
    # ------------------------------------------------------------------

    def _record_sync(
        self,
        user_id: Optional[int],
        media_type: MediaType,
        prompt: str,
        model: str,
        status: MediaStatus,
        result_url: Optional[str],
        image_count: int,
        task_id: Optional[str],
        size: Optional[str],
        resolution: Optional[str],
        duration: Optional[int],
        error_message: Optional[str],
    ) -> MediaHistory:
        def _block(session: Session) -> MediaHistory:
            record = MediaHistory(
                id=str(uuid.uuid4()),
                user_id=user_id,
                media_type=media_type,
                prompt=prompt,
                model=model,
                status=status,
                result_url=result_url,
                image_count=image_count,
                task_id=task_id,
                size=size,
                resolution=resolution,
                duration=duration,
                error_message=error_message,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

        return database_service.run_in_session(_block, op="media_record")

    def _get_history_by_user_sync(self, user_id: int, limit: int = 50) -> List[MediaHistory]:
        def _block(session: Session) -> List[MediaHistory]:
            result = session.exec(
                select(MediaHistory)
                .where(MediaHistory.user_id == user_id)
                .order_by(desc(MediaHistory.created_at))
                .limit(limit)
            )
            return list(result.all())

        return database_service.run_in_session(_block, op="media_get_history")

    # ------------------------------------------------------------------
    # Native async implementations (used when ENABLE_ASYNC_DB is on)
    # ------------------------------------------------------------------

    async def _record_async(
        self,
        user_id: Optional[int],
        media_type: MediaType,
        prompt: str,
        model: str,
        status: MediaStatus,
        result_url: Optional[str],
        image_count: int,
        task_id: Optional[str],
        size: Optional[str],
        resolution: Optional[str],
        duration: Optional[int],
        error_message: Optional[str],
    ) -> MediaHistory:
        async with get_async_session_maker()() as session:
            record = MediaHistory(
                id=str(uuid.uuid4()),
                user_id=user_id,
                media_type=media_type,
                prompt=prompt,
                model=model,
                status=status,
                result_url=result_url,
                image_count=image_count,
                task_id=task_id,
                size=size,
                resolution=resolution,
                duration=duration,
                error_message=error_message,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def _get_history_by_user_async(self, user_id: int, limit: int = 50) -> List[MediaHistory]:
        async with get_async_session_maker()() as session:
            result = await session.execute(
                select(MediaHistory)
                .where(MediaHistory.user_id == user_id)
                .order_by(desc(MediaHistory.created_at))
                .limit(limit)
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def record_generation(
        self,
        user_id: Optional[int],
        media_type: MediaType,
        prompt: str,
        model: str,
        status: MediaStatus,
        result_url: Optional[str] = None,
        image_count: int = 0,
        task_id: Optional[str] = None,
        size: Optional[str] = None,
        resolution: Optional[str] = None,
        duration: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> MediaHistory:
        """Record a media generation outcome (success or failure).

        History recording is best-effort: a DB failure is logged but does
        not propagate, so it never breaks the generation endpoint itself.
        """
        try:
            if settings.ENABLE_ASYNC_DB:
                return await self._record_async(
                    user_id,
                    media_type,
                    prompt,
                    model,
                    status,
                    result_url,
                    image_count,
                    task_id,
                    size,
                    resolution,
                    duration,
                    error_message,
                )
            return await run_in_thread(
                self._record_sync,
                user_id,
                media_type,
                prompt,
                model,
                status,
                result_url,
                image_count,
                task_id,
                size,
                resolution,
                duration,
                error_message,
                op="media_record",
            )
        except Exception as e:
            # Best-effort: never break the generation flow because history
            # recording failed. The generation itself already succeeded.
            logger.warning(
                "media_history_record_failed",
                user_id=user_id,
                media_type=media_type.value,
                status=status.value,
                error=str(e),
            )
            # Return a transient (unsaved) record so the caller has an object
            # to work with; the row simply won't be persisted.
            return MediaHistory(
                id="unsaved",
                user_id=user_id,
                media_type=media_type,
                prompt=prompt,
                model=model,
                status=status,
                result_url=result_url,
                image_count=image_count,
                task_id=task_id,
                size=size,
                resolution=resolution,
                duration=duration,
                error_message=error_message,
            )

    async def get_history_by_user(self, user_id: int, limit: int = 50) -> List[MediaHistory]:
        """Get a user's media generation history, newest first."""
        if settings.ENABLE_ASYNC_DB:
            return await self._get_history_by_user_async(user_id, limit)
        return await run_in_thread(self._get_history_by_user_sync, user_id, limit, op="media_get_history")


media_history_service = MediaHistoryService()
