"""Tests for GoalService business logic (build_context, get_progress).

These tests mock the DB layer (get_goal, get_tasks_by_goal) and
orchestrator.get_state to verify context assembly logic without
requiring a real database or checkpoint store.
"""

import json
from datetime import datetime, UTC
from unittest.mock import AsyncMock, patch

from app.models.goal import Goal
from app.models.sdlc_task import SDLCTask, SDLCTaskStatus
from app.services.goal_service import GoalService


def _make_goal(
    goal_id: str = "goal-1",
    user_id: int = 1,
    title: str = "Stock Analysis System",
    description: str = "Build a complete stock analysis toolchain",
) -> Goal:
    goal = Goal(id=goal_id, user_id=user_id, title=title, description=description)
    goal.created_at = datetime.now(UTC)
    goal.updated_at = datetime.now(UTC)
    return goal


def _make_task(
    task_id: str = "task-1",
    thread_id: str = "thread-1",
    status: SDLCTaskStatus = SDLCTaskStatus.COMPLETED,
    requirements: str = "Data collection module",
    plan: str = "[]",
    goal_id: str = "goal-1",
) -> SDLCTask:
    task = SDLCTask(
        id=task_id,
        thread_id=thread_id,
        user_id=1,
        status=status,
        requirements=requirements,
        plan=plan,
        goal_id=goal_id,
    )
    task.created_at = datetime.now(UTC)
    task.updated_at = datetime.now(UTC)
    return task


class TestBuildContext:
    """Tests for GoalService.build_context — the core cross-session capability."""

    async def test_build_context_assembles_history(self):
        """build_context returns Goal title + task requirements + plan summary."""
        svc = GoalService()
        goal = _make_goal()
        plan_json = json.dumps([{"description": "Fetch daily OHLCV data"}, {"description": "Store to PostgreSQL"}])
        tasks = [_make_task(requirements="Data collection module", plan=plan_json)]

        with (
            patch.object(svc, "get_goal", AsyncMock(return_value=goal)),
            patch.object(svc, "get_tasks_by_goal", AsyncMock(return_value=tasks)),
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
        ):
            mock_orch.get_state = AsyncMock(return_value={"status": "completed"})
            context = await svc.build_context("goal-1", 1)

        assert "Stock Analysis System" in context
        assert "Data collection module" in context
        assert "Fetch daily OHLCV data" in context
        assert "Latest Task State: completed" in context

    async def test_build_context_empty_goal(self):
        """build_context returns 'first task' hint when no tasks exist."""
        svc = GoalService()
        goal = _make_goal()

        with (
            patch.object(svc, "get_goal", AsyncMock(return_value=goal)),
            patch.object(svc, "get_tasks_by_goal", AsyncMock(return_value=[])),
        ):
            context = await svc.build_context("goal-1", 1)

        assert "first task" in context
        assert "Stock Analysis System" in context

    async def test_build_context_nonexistent_goal(self):
        """build_context returns empty string for nonexistent goal."""
        svc = GoalService()

        with patch.object(svc, "get_goal", AsyncMock(return_value=None)):
            context = await svc.build_context("nonexistent", 1)

        assert context == ""

    async def test_build_context_wrong_user(self):
        """build_context returns empty string if goal owned by another user."""
        svc = GoalService()
        goal = _make_goal(user_id=2)  # owned by user 2

        with patch.object(svc, "get_goal", AsyncMock(return_value=goal)):
            context = await svc.build_context("goal-1", user_id=1)  # queried by user 1

        assert context == ""


class TestGetProgress:
    """Tests for GoalService.get_progress — task counting logic."""

    async def test_get_progress_counts(self):
        """get_progress correctly counts completed/active/cancelled tasks."""
        svc = GoalService()
        tasks = [
            _make_task(task_id="t1", status=SDLCTaskStatus.COMPLETED),
            _make_task(task_id="t2", status=SDLCTaskStatus.COMPLETED),
            _make_task(task_id="t3", status=SDLCTaskStatus.CODING),
            _make_task(task_id="t4", status=SDLCTaskStatus.CANCELLED),
        ]

        with patch.object(svc, "get_tasks_by_goal", AsyncMock(return_value=tasks)):
            progress = await svc.get_progress("goal-1")

        assert progress["total"] == 4
        assert progress["completed"] == 2
        assert progress["active"] == 1
        assert progress["cancelled"] == 1
        assert progress["completion_ratio"] == 0.5

    async def test_get_progress_empty_goal(self):
        """get_progress returns zeros for a goal with no tasks."""
        svc = GoalService()

        with patch.object(svc, "get_tasks_by_goal", AsyncMock(return_value=[])):
            progress = await svc.get_progress("goal-1")

        assert progress["total"] == 0
        assert progress["completion_ratio"] == 0.0
