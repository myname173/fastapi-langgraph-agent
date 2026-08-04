"""Tests for Goal spec backfill in _persist_task_outcome.

Verifies that the PO agent's ProductSpec (problem_statement +
success_metrics) is correctly backfilled into the associated Goal's
description at the API layer, with proper skip conditions for fallback
specs, missing goal_id, and empty specs.

Also verifies that the "specing" status (from the PO phase) does not
crash _persist_task_outcome when SDLCTaskStatus is constructed.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.v1.sdlc import _persist_task_outcome
from app.models.sdlc_task import SDLCTaskStatus


def _real_spec_state(**overrides: Any) -> dict[str, Any]:
    """State with a real (non-fallback) ProductSpec."""
    state: dict[str, Any] = {
        "status": "completed",
        "plan": [{"id": "step-1", "description": "do it"}],
        "product_spec": [
            {
                "summary": "Build hello API",
                "problem_statement": "Users need a simple greeting endpoint",
                "success_metrics": ["GET /hello returns 200", "response time < 100ms"],
                "acceptance_criteria": [],
                "fallback": False,
            }
        ],
    }
    state.update(overrides)
    return state


def _fallback_spec_state(**overrides: Any) -> dict[str, Any]:
    """State with a fallback ProductSpec (PO failed)."""
    state: dict[str, Any] = {
        "status": "completed",
        "plan": [{"id": "step-1", "description": "do it"}],
        "product_spec": [{"fallback": True, "summary": "PO unavailable"}],
    }
    state.update(overrides)
    return state


def _mock_task(goal_id: str | None = "goal-123") -> MagicMock:
    """Mock SDLCTask with a goal_id."""
    task = MagicMock()
    task.goal_id = goal_id
    return task


class TestGoalSpecBackfill:
    """Tests for the Goal description backfill from ProductSpec."""

    async def test_real_spec_backfills_goal(self):
        """Real spec + task with goal_id → goal_service.update_goal called."""
        state = _real_spec_state()
        mock_task = _mock_task(goal_id="goal-abc")

        with (
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task_svc,
            patch("app.api.v1.sdlc.goal_service") as mock_goal_svc,
        ):
            mock_task_svc.update_task_status = AsyncMock()
            mock_task_svc.update_task_plan = AsyncMock()
            mock_task_svc.get_task = AsyncMock(return_value=mock_task)
            mock_goal_svc.update_goal = AsyncMock()

            await _persist_task_outcome("thread-001", state)

        # Goal was updated with problem_statement + success_metrics
        mock_goal_svc.update_goal.assert_awaited_once()
        _, kwargs = mock_goal_svc.update_goal.call_args
        assert kwargs["goal_id"] == "goal-abc"
        desc = kwargs["description"]
        assert "Users need a simple greeting endpoint" in desc
        assert "GET /hello returns 200" in desc
        assert "response time < 100ms" in desc

    async def test_fallback_spec_skips_backfill(self):
        """Fallback spec (PO failed) → goal_service.update_goal NOT called."""
        state = _fallback_spec_state()
        mock_task = _mock_task(goal_id="goal-abc")

        with (
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task_svc,
            patch("app.api.v1.sdlc.goal_service") as mock_goal_svc,
        ):
            mock_task_svc.update_task_status = AsyncMock()
            mock_task_svc.update_task_plan = AsyncMock()
            mock_task_svc.get_task = AsyncMock(return_value=mock_task)
            mock_goal_svc.update_goal = AsyncMock()

            await _persist_task_outcome("thread-001", state)

        mock_goal_svc.update_goal.assert_not_awaited()

    async def test_no_goal_id_skips_backfill(self):
        """Real spec but task.goal_id=None → goal_service.update_goal NOT called."""
        state = _real_spec_state()
        mock_task = _mock_task(goal_id=None)

        with (
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task_svc,
            patch("app.api.v1.sdlc.goal_service") as mock_goal_svc,
        ):
            mock_task_svc.update_task_status = AsyncMock()
            mock_task_svc.update_task_plan = AsyncMock()
            mock_task_svc.get_task = AsyncMock(return_value=mock_task)
            mock_goal_svc.update_goal = AsyncMock()

            await _persist_task_outcome("thread-001", state)

        mock_goal_svc.update_goal.assert_not_awaited()

    async def test_empty_spec_skips_backfill(self):
        """Empty product_spec → goal_service.update_goal NOT called."""
        state = _real_spec_state(product_spec=[])

        with (
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task_svc,
            patch("app.api.v1.sdlc.goal_service") as mock_goal_svc,
        ):
            mock_task_svc.update_task_status = AsyncMock()
            mock_task_svc.update_task_plan = AsyncMock()
            mock_task_svc.get_task = AsyncMock(return_value=_mock_task())
            mock_goal_svc.update_goal = AsyncMock()

            await _persist_task_outcome("thread-001", state)

        mock_goal_svc.update_goal.assert_not_awaited()
        # get_task should not even be called when spec is empty
        mock_task_svc.get_task.assert_not_awaited()

    async def test_specing_status_does_not_crash(self):
        """status='specing' is a valid SDLCTaskStatus after Phase 4 enum update."""
        state = _real_spec_state(status="specing")

        with (
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task_svc,
            patch("app.api.v1.sdlc.goal_service") as mock_goal_svc,
        ):
            mock_task_svc.update_task_status = AsyncMock()
            mock_task_svc.update_task_plan = AsyncMock()
            mock_task_svc.get_task = AsyncMock(return_value=_mock_task())
            mock_goal_svc.update_goal = AsyncMock()

            # Should not raise ValueError on SDLCTaskStatus("specing")
            await _persist_task_outcome("thread-001", state)

        mock_task_svc.update_task_status.assert_awaited_once_with("thread-001", SDLCTaskStatus("specing"))

    async def test_backfill_failure_is_best_effort(self):
        """If goal_service.update_goal raises, the error is swallowed (best-effort)."""
        state = _real_spec_state()
        mock_task = _mock_task(goal_id="goal-abc")

        with (
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task_svc,
            patch("app.api.v1.sdlc.goal_service") as mock_goal_svc,
        ):
            mock_task_svc.update_task_status = AsyncMock()
            mock_task_svc.update_task_plan = AsyncMock()
            mock_task_svc.get_task = AsyncMock(return_value=mock_task)
            mock_goal_svc.update_goal = AsyncMock(side_effect=RuntimeError("DB down"))

            # Should not raise — _persist_task_outcome swallows errors
            await _persist_task_outcome("thread-001", state)

        mock_goal_svc.update_goal.assert_awaited_once()
