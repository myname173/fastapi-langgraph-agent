"""Goal Service for managing cross-session long-term objectives."""

import json
from datetime import datetime, UTC
from typing import List, Optional

from sqlmodel import Session, desc, select

from app.core.db.session import get_async_session_maker
from app.core.db_executor import run_in_thread
from app.core.logging import logger
from app.models.goal import Goal, GoalStatus
from app.models.sdlc_task import SDLCTask, SDLCTaskStatus
from app.services.database import database_service
from app.services.sdlc_task_service import sdlc_task_service


class GoalService:
    """Service for managing long-term goals and task association."""

    # ------------------------------------------------------------------
    # Sync DB blocks (private) — dispatched off-loop via run_in_thread
    # ------------------------------------------------------------------

    def _create_goal_sync(self, goal_id: str, user_id: int, title: str, description: str) -> Goal:
        def _block(session: Session) -> Goal:
            goal = Goal(
                id=goal_id,
                user_id=user_id,
                title=title,
                description=description,
                status=GoalStatus.ACTIVE,
            )
            session.add(goal)
            session.commit()
            session.refresh(goal)
            logger.info("goal_created", goal_id=goal_id, user_id=user_id)
            return goal

        return database_service.run_in_session(_block, op="goal_create")

    def _get_goal_sync(self, goal_id: str) -> Optional[Goal]:
        def _block(session: Session) -> Optional[Goal]:
            result = session.exec(select(Goal).where(Goal.id == goal_id))
            return result.first()

        return database_service.run_in_session(_block, op="goal_get")

    def _get_goals_by_user_sync(self, user_id: int) -> List[Goal]:
        def _block(session: Session) -> List[Goal]:
            result = session.exec(select(Goal).where(Goal.user_id == user_id).order_by(desc(Goal.updated_at)))
            return list(result.all())

        return database_service.run_in_session(_block, op="goal_get_by_user")

    def _update_goal_sync(
        self,
        goal_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[GoalStatus] = None,
    ) -> Optional[Goal]:
        def _block(session: Session) -> Optional[Goal]:
            result = session.exec(select(Goal).where(Goal.id == goal_id))
            goal = result.first()
            if not goal:
                return None
            if title is not None:
                goal.title = title
            if description is not None:
                goal.description = description
            if status is not None:
                goal.status = status
            goal.updated_at = datetime.now(UTC)
            if status == GoalStatus.COMPLETED and not goal.completed_at:
                goal.completed_at = datetime.now(UTC)
            session.commit()
            session.refresh(goal)
            logger.info("goal_updated", goal_id=goal_id)
            return goal

        return database_service.run_in_session(_block, op="goal_update")

    def _delete_goal_sync(self, goal_id: str) -> bool:
        def _block(session: Session) -> bool:
            result = session.exec(select(Goal).where(Goal.id == goal_id))
            goal = result.first()
            if not goal:
                return False
            session.delete(goal)
            session.commit()
            logger.info("goal_deleted", goal_id=goal_id)
            return True

        return database_service.run_in_session(_block, op="goal_delete")

    def _get_tasks_by_goal_sync(self, goal_id: str) -> List[SDLCTask]:
        def _block(session: Session) -> List[SDLCTask]:
            result = session.exec(
                select(SDLCTask).where(SDLCTask.goal_id == goal_id).order_by(desc(SDLCTask.created_at))
            )
            return list(result.all())

        return database_service.run_in_session(_block, op="goal_get_tasks")

    # ------------------------------------------------------------------
    # Native async implementations (used when ENABLE_ASYNC_DB is on)
    # ------------------------------------------------------------------

    async def _create_goal_async(self, goal_id: str, user_id: int, title: str, description: str) -> Goal:
        async with get_async_session_maker()() as session:
            goal = Goal(
                id=goal_id,
                user_id=user_id,
                title=title,
                description=description,
                status=GoalStatus.ACTIVE,
            )
            session.add(goal)
            await session.commit()
            await session.refresh(goal)
            logger.info("goal_created", goal_id=goal_id, user_id=user_id)
            return goal

    async def _get_goal_async(self, goal_id: str) -> Optional[Goal]:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(Goal).where(Goal.id == goal_id))
            return result.scalars().first()

    async def _get_goals_by_user_async(self, user_id: int) -> List[Goal]:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(Goal).where(Goal.user_id == user_id).order_by(desc(Goal.updated_at)))
            return list(result.scalars().all())

    async def _update_goal_async(
        self,
        goal_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[GoalStatus] = None,
    ) -> Optional[Goal]:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(Goal).where(Goal.id == goal_id))
            goal = result.scalars().first()
            if not goal:
                return None
            if title is not None:
                goal.title = title
            if description is not None:
                goal.description = description
            if status is not None:
                goal.status = status
            goal.updated_at = datetime.now(UTC)
            if status == GoalStatus.COMPLETED and not goal.completed_at:
                goal.completed_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(goal)
            logger.info("goal_updated", goal_id=goal_id)
            return goal

    async def _delete_goal_async(self, goal_id: str) -> bool:
        async with get_async_session_maker()() as session:
            result = await session.execute(select(Goal).where(Goal.id == goal_id))
            goal = result.scalars().first()
            if not goal:
                return False
            await session.delete(goal)
            await session.commit()
            logger.info("goal_deleted", goal_id=goal_id)
            return True

    async def _get_tasks_by_goal_async(self, goal_id: str) -> List[SDLCTask]:
        async with get_async_session_maker()() as session:
            result = await session.execute(
                select(SDLCTask).where(SDLCTask.goal_id == goal_id).order_by(desc(SDLCTask.created_at))
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Public async API — CRUD
    # ------------------------------------------------------------------

    async def create_goal(self, goal_id: str, user_id: int, title: str, description: str = "") -> Goal:
        """Create a new long-term goal."""
        from app.core.config import settings

        if settings.ENABLE_ASYNC_DB:
            return await self._create_goal_async(goal_id, user_id, title, description)
        return await run_in_thread(self._create_goal_sync, goal_id, user_id, title, description, op="goal_create")

    async def get_goal(self, goal_id: str) -> Optional[Goal]:
        """Get a goal by ID."""
        from app.core.config import settings

        if settings.ENABLE_ASYNC_DB:
            return await self._get_goal_async(goal_id)
        return await run_in_thread(self._get_goal_sync, goal_id, op="goal_get")

    async def get_goals_by_user(self, user_id: int) -> List[Goal]:
        """Get all goals for a user, newest-updated first."""
        from app.core.config import settings

        if settings.ENABLE_ASYNC_DB:
            return await self._get_goals_by_user_async(user_id)
        return await run_in_thread(self._get_goals_by_user_sync, user_id, op="goal_get_by_user")

    async def update_goal(
        self,
        goal_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[GoalStatus] = None,
    ) -> Optional[Goal]:
        """Update goal title/description/status.

        Only non-None fields are updated. ``id`` and ``user_id`` are immutable.
        """
        from app.core.config import settings

        if settings.ENABLE_ASYNC_DB:
            return await self._update_goal_async(goal_id, title, description, status)
        return await run_in_thread(self._update_goal_sync, goal_id, title, description, status, op="goal_update")

    async def delete_goal(self, goal_id: str) -> bool:
        """Delete a goal. Callers should dissociate tasks first."""
        from app.core.config import settings

        if settings.ENABLE_ASYNC_DB:
            return await self._delete_goal_async(goal_id)
        return await run_in_thread(self._delete_goal_sync, goal_id, op="goal_delete")

    # ------------------------------------------------------------------
    # Task association + progress
    # ------------------------------------------------------------------

    async def get_tasks_by_goal(self, goal_id: str) -> List[SDLCTask]:
        """List all SDLCTasks associated with a Goal, newest first."""
        from app.core.config import settings

        if settings.ENABLE_ASYNC_DB:
            return await self._get_tasks_by_goal_async(goal_id)
        return await run_in_thread(self._get_tasks_by_goal_sync, goal_id, op="goal_get_tasks")

    async def associate_task(self, goal_id: str, task_id: str) -> Optional[SDLCTask]:
        """Associate an existing SDLCTask with a Goal (sets task.goal_id)."""
        return await sdlc_task_service.update_task_goal(task_id, goal_id)

    async def dissociate_task(self, task_id: str) -> Optional[SDLCTask]:
        """Remove a task's Goal association (sets task.goal_id = None)."""
        return await sdlc_task_service.update_task_goal(task_id, None)

    async def get_progress(self, goal_id: str) -> dict:
        """Return progress stats for a Goal.

        Returns:
            dict with total, completed, active, cancelled counts + ratio.
        """
        tasks = await self.get_tasks_by_goal(goal_id)
        total = len(tasks)
        completed = sum(1 for t in tasks if t.status == SDLCTaskStatus.COMPLETED)
        cancelled = sum(1 for t in tasks if t.status == SDLCTaskStatus.CANCELLED)
        active = total - completed - cancelled
        return {
            "total": total,
            "completed": completed,
            "active": active,
            "cancelled": cancelled,
            "completion_ratio": round(completed / total, 2) if total else 0.0,
        }

    # ------------------------------------------------------------------
    # Context assembly (core capability for cross-session injection)
    # ------------------------------------------------------------------

    async def build_context(self, goal_id: str, user_id: int) -> str:
        """Build a context string from a Goal's history for a new SDLC task.

        Assembles:
        - Goal title + description (the objective)
        - Associated tasks' requirements (user intents)
        - Completed tasks' plan summaries (what was done)
        - Latest task's checkpoint status snapshot (current state)

        This is injected into SDLCRequest.project_context at the API layer,
        so planner_node receives the context without any DB I/O.

        Args:
            goal_id: The Goal whose context to assemble.
            user_id: The user ID (for ownership verification).

        Returns:
            A markdown-formatted context string, or "" if Goal not found
            or not owned by the user.
        """
        goal = await self.get_goal(goal_id)
        if not goal or goal.user_id != user_id:
            return ""

        tasks = await self.get_tasks_by_goal(goal_id)
        if not tasks:
            return f"# Goal: {goal.title}\n\n{goal.description}\n\n(This is the first task for this goal.)"

        lines = [f"# Goal: {goal.title}", "", goal.description, "", "## Task History:", ""]

        for i, task in enumerate(tasks, 1):
            status_emoji = "✅" if task.status == SDLCTaskStatus.COMPLETED else "🔄"
            lines.append(f"### Task {i}: {task.requirements[:80]}")
            lines.append(f"Status: {status_emoji} {task.status.value}")
            lines.append(f"Thread: `{task.thread_id}`")

            # Include plan summary for completed tasks (what was done)
            if task.status == SDLCTaskStatus.COMPLETED and task.plan and task.plan != "[]":
                try:
                    plan_steps = json.loads(task.plan)
                    if plan_steps:
                        lines.append("Plan:")
                        for step in plan_steps[:5]:  # Cap at 5 steps to bound context size
                            desc = step.get("description", str(step))[:100]
                            lines.append(f"  - {desc}")
                except (json.JSONDecodeError, AttributeError, TypeError):
                    pass  # Skip malformed plan
            lines.append("")

        # Latest task's checkpoint state snapshot (read-only, does not write)
        latest = tasks[0]  # tasks are ordered newest-first
        try:
            from app.api.v1.sdlc import orchestrator

            latest_state = await orchestrator.get_state(session_id=latest.thread_id)
            if latest_state:
                latest_status = latest_state.get("status", "unknown")
                lines.append(f"## Latest Task State: {latest_status}")
        except Exception as e:
            logger.debug("goal_context_checkpoint_skip", error=str(e))

        return "\n".join(lines)


goal_service = GoalService()
