"""SDLC Task Service for managing SDLC workflow tasks."""

import shutil
from datetime import datetime, timedelta, UTC
from typing import List, Optional

from sqlmodel import Session, desc, select

from app.core.config import settings
from app.core.db.session import get_async_session_maker
from app.core.db_executor import run_in_thread
from app.core.logging import logger
from app.core.metrics import sdlc_cleanup_count
from app.models.sdlc_task import SDLCTask, SDLCTaskStatus
from app.services.database import database_service
from app.services.image_service import image_service
from app.services.knowledge_graph_service import knowledge_graph_service
from app.services.quota_service import quota_service


class SDLCTaskService:
    """Service for managing SDLC workflow tasks."""

    # ------------------------------------------------------------------
    # Sync DB blocks (private) — dispatched off-loop via run_in_thread
    # ------------------------------------------------------------------

    def _create_task_sync(
        self,
        task_id: str,
        thread_id: str,
        user_id: Optional[int],
        requirements: str,
        goal_id: Optional[str] = None,
        coder_model: Optional[str] = None,
    ) -> SDLCTask:
        def _block(session: Session) -> SDLCTask:
            task = SDLCTask(
                id=task_id,
                thread_id=thread_id,
                user_id=user_id,
                status=SDLCTaskStatus.PLANNING,
                requirements=requirements,
                goal_id=goal_id,
                coder_model=coder_model,
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            logger.info("sdlc_task_created", task_id=task_id, thread_id=thread_id)
            return task

        return database_service.run_in_session(_block, op="sdlc_create_task")

    def _get_task_sync(self, task_id: str) -> Optional[SDLCTask]:
        def _block(session: Session) -> Optional[SDLCTask]:
            result = session.exec(select(SDLCTask).where(SDLCTask.id == task_id))
            return result.first()

        return database_service.run_in_session(_block, op="sdlc_get_task")

    def _get_tasks_by_user_sync(self, user_id: int) -> List[SDLCTask]:
        def _block(session: Session) -> List[SDLCTask]:
            result = session.exec(
                select(SDLCTask).where(SDLCTask.user_id == user_id).order_by(desc(SDLCTask.created_at))
            )
            return list(result.all())

        return database_service.run_in_session(_block, op="sdlc_get_tasks_by_user")

    def _get_all_tasks_sync(self) -> List[SDLCTask]:
        def _block(session: Session) -> List[SDLCTask]:
            result = session.exec(select(SDLCTask).order_by(desc(SDLCTask.created_at)))
            return list(result.all())

        return database_service.run_in_session(_block, op="sdlc_get_all_tasks")

    def _update_task_status_sync(self, task_id: str, status: SDLCTaskStatus) -> Optional[SDLCTask]:
        def _block(session: Session) -> Optional[SDLCTask]:
            result = session.exec(select(SDLCTask).where(SDLCTask.id == task_id))
            task = result.first()

            if not task:
                logger.warning("sdlc_task_not_found", task_id=task_id)
                return None

            task.status = status
            task.updated_at = datetime.now(UTC)

            if status == SDLCTaskStatus.COMPLETED:
                task.completed_at = datetime.now(UTC)

            session.commit()
            session.refresh(task)

            logger.info("sdlc_task_status_updated", task_id=task_id, status=status)
            return task

        return database_service.run_in_session(_block, op="sdlc_update_task_status")

    def _update_task_plan_sync(self, task_id: str, plan: str) -> Optional[SDLCTask]:
        def _block(session: Session) -> Optional[SDLCTask]:
            result = session.exec(select(SDLCTask).where(SDLCTask.id == task_id))
            task = result.first()

            if not task:
                logger.warning("sdlc_task_not_found", task_id=task_id)
                return None

            task.plan = plan
            task.updated_at = datetime.now(UTC)

            session.commit()
            session.refresh(task)

            logger.info("sdlc_task_plan_updated", task_id=task_id)
            return task

        return database_service.run_in_session(_block, op="sdlc_update_task_plan")

    def _update_task_goal_sync(self, task_id: str, goal_id: Optional[str]) -> Optional[SDLCTask]:
        """Sync: associate or dissociate a task with a Goal."""

        def _block(session: Session) -> Optional[SDLCTask]:
            result = session.exec(select(SDLCTask).where(SDLCTask.id == task_id))
            task = result.first()
            if not task:
                logger.warning("sdlc_task_not_found", task_id=task_id)
                return None
            task.goal_id = goal_id
            task.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(task)
            logger.info("sdlc_task_goal_updated", task_id=task_id, goal_id=goal_id)
            return task

        return database_service.run_in_session(_block, op="sdlc_update_task_goal")

    def _delete_task_sync(self, task_id: str) -> Optional[str]:
        """Delete a task row, returning its thread_id for resource cleanup.

        Returns:
            The thread_id if the task existed, else ``None``.
        """

        def _block(session: Session) -> Optional[str]:
            result = session.exec(select(SDLCTask).where(SDLCTask.id == task_id))
            task = result.first()

            if not task:
                logger.warning("sdlc_task_not_found", task_id=task_id)
                return None

            thread_id = task.thread_id
            session.delete(task)
            session.commit()

            logger.info("sdlc_task_deleted", task_id=task_id)
            return thread_id

        return database_service.run_in_session(_block, op="sdlc_delete_task")

    # ------------------------------------------------------------------
    # Native async implementations (used when ENABLE_ASYNC_DB is on)
    # ------------------------------------------------------------------

    async def _create_task_async(
        self,
        task_id: str,
        thread_id: str,
        user_id: Optional[int],
        requirements: str,
        goal_id: Optional[str] = None,
        coder_model: Optional[str] = None,
    ) -> SDLCTask:
        async with get_async_session_maker()() as session:
            task = SDLCTask(
                id=task_id,
                thread_id=thread_id,
                user_id=user_id,
                status=SDLCTaskStatus.PLANNING,
                requirements=requirements,
                goal_id=goal_id,
                coder_model=coder_model,
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            logger.info("sdlc_task_created", task_id=task_id, thread_id=thread_id)
            return task

    async def _get_task_async(self, task_id: str) -> Optional[SDLCTask]:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(SDLCTask).where(SDLCTask.id == task_id))
            return result.scalars().first()

    async def _get_tasks_by_user_async(self, user_id: int) -> List[SDLCTask]:
        async with get_async_session_maker()() as session:
            # ISSUE-10 fix: order by created_at desc (newest first) for
            # consistent sidebar display — previously unordered.
            result = await session.execute(
                select(SDLCTask).where(SDLCTask.user_id == user_id).order_by(desc(SDLCTask.created_at))
            )
            return list(result.scalars().all())

    async def _get_all_tasks_async(self) -> List[SDLCTask]:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(SDLCTask).order_by(desc(SDLCTask.created_at)))
            return list(result.scalars().all())

    async def _update_task_status_async(self, task_id: str, status: SDLCTaskStatus) -> Optional[SDLCTask]:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(SDLCTask).where(SDLCTask.id == task_id))
            task = result.scalars().first()

            if not task:
                logger.warning("sdlc_task_not_found", task_id=task_id)
                return None

            task.status = status
            task.updated_at = datetime.now(UTC)

            if status == SDLCTaskStatus.COMPLETED:
                task.completed_at = datetime.now(UTC)

            await session.commit()
            await session.refresh(task)

            logger.info("sdlc_task_status_updated", task_id=task_id, status=status)
            return task

    async def _update_task_plan_async(self, task_id: str, plan: str) -> Optional[SDLCTask]:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(SDLCTask).where(SDLCTask.id == task_id))
            task = result.scalars().first()

            if not task:
                logger.warning("sdlc_task_not_found", task_id=task_id)
                return None

            task.plan = plan
            task.updated_at = datetime.now(UTC)

            await session.commit()
            await session.refresh(task)

            logger.info("sdlc_task_plan_updated", task_id=task_id)
            return task

    async def _update_task_goal_async(self, task_id: str, goal_id: Optional[str]) -> Optional[SDLCTask]:
        """Async: associate or dissociate a task with a Goal."""
        async with get_async_session_maker()() as session:
            result = await session.execute(select(SDLCTask).where(SDLCTask.id == task_id))
            task = result.scalars().first()
            if not task:
                logger.warning("sdlc_task_not_found", task_id=task_id)
                return None
            task.goal_id = goal_id
            task.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(task)
            logger.info("sdlc_task_goal_updated", task_id=task_id, goal_id=goal_id)
            return task

    async def _delete_task_async(self, task_id: str) -> Optional[str]:
        """Delete a task row, returning its thread_id for resource cleanup."""
        async with get_async_session_maker()() as session:
            result = await session.execute(select(SDLCTask).where(SDLCTask.id == task_id))
            task = result.scalars().first()

            if not task:
                logger.warning("sdlc_task_not_found", task_id=task_id)
                return None

            thread_id = task.thread_id
            await session.delete(task)
            await session.commit()

            logger.info("sdlc_task_deleted", task_id=task_id)
            return thread_id

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def create_task(
        self,
        task_id: str,
        thread_id: str,
        user_id: Optional[int] = None,
        requirements: str = "",
        goal_id: Optional[str] = None,
        coder_model: Optional[str] = None,
    ) -> SDLCTask:
        """Create a new SDLC task.

        Args:
            task_id: Stable identifier for the task, used by the frontend and
                the agent graph to refer to the same run.
            thread_id: LangGraph thread this task is checkpointed under.
            user_id: Owning user. ``None`` skips the per-user quota gate.
            requirements: Raw requirement text the agent plans against.
            goal_id: Optional investment goal this task is attached to.
            coder_model: Coder model the run is being launched with, recorded
                so the task list can display it (NULL when unset/default).

        Returns:
            The persisted ``SDLCTask`` row.

        Raises:
            ValueError: If user has exceeded active task quota.
        """
        # M-15 fix: use the TOCTOU-safe reserve_task_slot context manager
        # instead of check_task_count_quota + separate create. The per-user
        # asyncio.Lock is held across both the count check and the insert,
        # so concurrent create_task calls for the same user cannot both
        # pass the quota gate. ValueError is raised by reserve_task_slot
        # at entry when over quota, preserving the old error contract.
        async with quota_service.reserve_task_slot(user_id):
            if settings.ENABLE_ASYNC_DB:
                return await self._create_task_async(task_id, thread_id, user_id, requirements, goal_id, coder_model)
            return await run_in_thread(
                self._create_task_sync,
                task_id,
                thread_id,
                user_id,
                requirements,
                goal_id,
                coder_model,
                op="sdlc_create_task",
            )

    async def get_task(self, task_id: str) -> Optional[SDLCTask]:
        """Get a task by ID."""
        if settings.ENABLE_ASYNC_DB:
            return await self._get_task_async(task_id)
        return await run_in_thread(self._get_task_sync, task_id, op="sdlc_get_task")

    async def get_tasks_by_user(self, user_id: int) -> List[SDLCTask]:
        """Get all tasks for a user."""
        if settings.ENABLE_ASYNC_DB:
            return await self._get_tasks_by_user_async(user_id)
        return await run_in_thread(self._get_tasks_by_user_sync, user_id, op="sdlc_get_tasks_by_user")

    async def get_all_tasks(self) -> List[SDLCTask]:
        """Get all tasks (admin view)."""
        if settings.ENABLE_ASYNC_DB:
            return await self._get_all_tasks_async()
        return await run_in_thread(self._get_all_tasks_sync, op="sdlc_get_all_tasks")

    async def cleanup_task_resources(self, thread_id: str) -> None:
        """Clean up all temporary resources associated with a task.

        Removes:
        - Uploaded images in workspace/images/{thread_id}/
        - Knowledge graph JSON in logs/knowledge_graphs/{thread_id}.json
        - In-memory graph and hash data
        - Generated scripts in workspace/{thread_id}/scripts/
        - Output files in workspace/{thread_id}/outputs/

        Args:
            thread_id: Thread ID for resource isolation
        """
        try:
            image_count = await image_service.cleanup_task_images(thread_id)
            await knowledge_graph_service.cleanup_graph(thread_id)

            script_count = self._cleanup_scripts_and_outputs(thread_id)

            sdlc_cleanup_count.labels(resource_type="images").inc(image_count)
            sdlc_cleanup_count.labels(resource_type="scripts").inc(script_count)

            logger.info(
                "task_resources_cleaned_up",
                thread_id=thread_id,
                images_removed=image_count,
                scripts_removed=script_count,
            )
        except Exception as e:
            logger.error(
                "task_resource_cleanup_failed",
                thread_id=thread_id,
                error=str(e),
            )

    def _cleanup_scripts_and_outputs(self, thread_id: str) -> int:
        """Remove scripts/ and outputs/ directories for a task.

        Args:
            thread_id: Thread ID for workspace isolation

        Returns:
            Number of files removed
        """
        removed = 0
        workspace_dir = settings.SDLC_WORKSPACE_ROOT / thread_id

        for subdir in ("scripts", "outputs"):
            dir_path = workspace_dir / subdir
            if dir_path.exists():
                try:
                    file_count = sum(1 for f in dir_path.rglob("*") if f.is_file())
                    shutil.rmtree(dir_path)
                    removed += file_count
                except Exception as e:
                    logger.warning(
                        "script_cleanup_failed",
                        thread_id=thread_id,
                        subdir=subdir,
                        error=str(e),
                    )

        return removed

    async def update_task_status(self, task_id: str, status: SDLCTaskStatus) -> Optional[SDLCTask]:
        """Update task status.

        Triggers resource cleanup on terminal states (COMPLETED, CANCELLED).
        """
        if settings.ENABLE_ASYNC_DB:
            task = await self._update_task_status_async(task_id, status)
        else:
            task = await run_in_thread(self._update_task_status_sync, task_id, status, op="sdlc_update_task_status")

        if task and status in (SDLCTaskStatus.COMPLETED, SDLCTaskStatus.CANCELLED):
            await self.cleanup_task_resources(task.thread_id)

        return task

    async def update_task_plan(self, task_id: str, plan: str) -> Optional[SDLCTask]:
        """Update task plan."""
        if settings.ENABLE_ASYNC_DB:
            return await self._update_task_plan_async(task_id, plan)
        return await run_in_thread(self._update_task_plan_sync, task_id, plan, op="sdlc_update_task_plan")

    async def update_task_goal(self, task_id: str, goal_id: Optional[str]) -> Optional[SDLCTask]:
        """Associate or dissociate a task with a Goal.

        Args:
            task_id: The SDLC task ID.
            goal_id: The Goal ID to associate, or None to dissociate.
        """
        if settings.ENABLE_ASYNC_DB:
            return await self._update_task_goal_async(task_id, goal_id)
        return await run_in_thread(self._update_task_goal_sync, task_id, goal_id, op="sdlc_update_task_goal")

    async def cancel_task(self, task_id: str) -> Optional[SDLCTask]:
        """Cancel a task."""
        return await self.update_task_status(task_id, SDLCTaskStatus.CANCELLED)

    async def pause_task(self, task_id: str) -> Optional[SDLCTask]:
        """Pause a task."""
        return await self.update_task_status(task_id, SDLCTaskStatus.PAUSED)

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task and clean up all associated resources."""
        if settings.ENABLE_ASYNC_DB:
            thread_id = await self._delete_task_async(task_id)
        else:
            thread_id = await run_in_thread(self._delete_task_sync, task_id, op="sdlc_delete_task")
        if thread_id is None:
            return False

        await self.cleanup_task_resources(thread_id)
        return True

    # ------------------------------------------------------------------
    # Stale-task reaping
    # ------------------------------------------------------------------

    #: Statuses meaning "still in flight". ``PAUSED`` is deliberately excluded
    #: — it is a state the user chose, not evidence of an interrupted run.
    ACTIVE_STATUSES: tuple = (
        SDLCTaskStatus.SPECING,
        SDLCTaskStatus.PLANNING,
        SDLCTaskStatus.CODING,
        SDLCTaskStatus.TESTING,
        SDLCTaskStatus.REVIEWING,
    )

    def _reap_stale_tasks_sync(self, timeout_minutes: int) -> int:
        """Mark non-terminal tasks not updated within the timeout as FAILED.

        An interrupted run (process restart, crash, aborted SSE stream) never
        writes a terminal status, so its row sits in 「进行中」 forever. Nothing
        else in the system transitions it, which is why the list accumulated
        tasks that had been dead for weeks.

        Args:
            timeout_minutes: Age (by ``updated_at``) past which a non-terminal
                task is considered dead.

        Returns:
            Number of tasks reaped.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=timeout_minutes)

        def _block(session: Session) -> int:
            result = session.exec(
                select(SDLCTask).where(
                    # Pass the enum members. The column is a SQLAlchemy Enum
                    # whose value set is the member *names* ('PLANNING', ...);
                    # the bind processor normalises members (and their .value
                    # strings) to those names, so this matches what is stored.
                    SDLCTask.status.in_(self.ACTIVE_STATUSES),
                    SDLCTask.updated_at < cutoff,
                )
            )
            stale = list(result.all())
            now = datetime.now(UTC)
            for task in stale:
                previous_status = task.status
                task.status = SDLCTaskStatus.FAILED
                task.updated_at = now
                session.add(task)
                logger.warning(
                    "sdlc_stale_task_reaped",
                    task_id=task.id,
                    previous_status=str(previous_status),
                    timeout_minutes=timeout_minutes,
                )
            if stale:
                session.commit()
            return len(stale)

        return database_service.run_in_session(_block, op="sdlc_reap_stale_tasks")

    async def _reap_stale_tasks_async(self, timeout_minutes: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(minutes=timeout_minutes)
        async with get_async_session_maker()() as session:
            result = await session.execute(
                select(SDLCTask).where(
                    SDLCTask.status.in_(self.ACTIVE_STATUSES),
                    SDLCTask.updated_at < cutoff,
                )
            )
            stale = list(result.scalars().all())
            now = datetime.now(UTC)
            for task in stale:
                previous_status = task.status
                task.status = SDLCTaskStatus.FAILED
                task.updated_at = now
                session.add(task)
                logger.warning(
                    "sdlc_stale_task_reaped",
                    task_id=task.id,
                    previous_status=str(previous_status),
                    timeout_minutes=timeout_minutes,
                )
            if stale:
                await session.commit()
            return len(stale)

    async def reap_stale_tasks(self, timeout_minutes: Optional[int] = None) -> int:
        """Reap interrupted tasks left in a non-terminal status.

        Best-effort: a failure here must never prevent the app from starting.

        Args:
            timeout_minutes: Override the configured threshold. ``0`` disables.

        Returns:
            Number of tasks reaped (0 on any failure).
        """
        minutes = settings.SDLC_STALE_TASK_TIMEOUT_MINUTES if timeout_minutes is None else timeout_minutes
        if minutes <= 0:
            return 0
        try:
            if settings.ENABLE_ASYNC_DB:
                reaped = await self._reap_stale_tasks_async(minutes)
            else:
                reaped = await run_in_thread(self._reap_stale_tasks_sync, minutes, op="sdlc_reap_stale_tasks")
            if reaped:
                sdlc_cleanup_count.labels(resource_type="stale_tasks").inc(reaped)
                logger.info("sdlc_stale_tasks_reaped", count=reaped, timeout_minutes=minutes)
            return reaped
        except Exception as e:
            logger.exception("sdlc_stale_task_reap_failed", error=str(e))
            return 0


sdlc_task_service = SDLCTaskService()
