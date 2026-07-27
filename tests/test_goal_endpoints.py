"""Tests for Goal API endpoints.

Goal endpoints use ``get_current_user`` (not ``get_current_session``),
so we mock ``db_service.get_user`` and mint a user-type JWT token.
``goal_service`` is mocked at the module level to avoid DB I/O.
"""

from datetime import datetime, UTC
from unittest.mock import AsyncMock, patch


from app.models.goal import Goal, GoalStatus
from app.models.sdlc_task import SDLCTask, SDLCTaskStatus


def _make_user_token(user_id: int = 1) -> str:
    """Mint a user-type JWT token for get_current_user."""
    from app.utils.auth import create_access_token

    token = create_access_token(str(user_id), token_type="user")
    return token.access_token


def _make_fake_goal(
    goal_id: str = "goal-1",
    user_id: int = 1,
    title: str = "Test Goal",
    description: str = "A test goal",
    status: GoalStatus = GoalStatus.ACTIVE,
) -> Goal:
    """Build a Goal object without DB persistence."""
    now = datetime.now(UTC)
    goal = Goal(
        id=goal_id,
        user_id=user_id,
        title=title,
        description=description,
        status=status,
    )
    goal.created_at = now
    goal.updated_at = now
    return goal


def _make_fake_task(
    task_id: str = "task-1",
    thread_id: str = "thread-1",
    user_id: int = 1,
    status: SDLCTaskStatus = SDLCTaskStatus.COMPLETED,
    requirements: str = "Build data collection module",
    goal_id: str = "goal-1",
) -> SDLCTask:
    """Build an SDLCTask object without DB persistence."""
    task = SDLCTask(
        id=task_id,
        thread_id=thread_id,
        user_id=user_id,
        status=status,
        requirements=requirements,
        goal_id=goal_id,
    )
    task.created_at = datetime.now(UTC)
    task.updated_at = datetime.now(UTC)
    return task


class TestGoalCRUD:
    """Tests for Goal CRUD endpoints."""

    async def test_create_goal_returns_200(self, async_client, mock_db, make_fake_user):
        """POST /goals creates a goal and returns 200."""
        fake_user = make_fake_user(user_id=1)
        mock_db.get_user.return_value = fake_user
        headers = {"Authorization": f"Bearer {_make_user_token(1)}"}
        fake_goal = _make_fake_goal()

        with patch("app.api.v1.goals.goal_service") as mock_svc:
            mock_svc.create_goal = AsyncMock(return_value=fake_goal)
            response = await async_client.post(
                "/api/v1/goals/",
                headers=headers,
                json={"title": "Test Goal", "description": "A test goal"},
            )

        assert response.status_code == 200, response.text
        assert response.json()["title"] == "Test Goal"
        mock_svc.create_goal.assert_called_once()

    async def test_list_goals_returns_user_goals(self, async_client, mock_db, make_fake_user):
        """GET /goals returns only the current user's goals."""
        fake_user = make_fake_user(user_id=1)
        mock_db.get_user.return_value = fake_user
        headers = {"Authorization": f"Bearer {_make_user_token(1)}"}
        goals = [_make_fake_goal(goal_id="g1"), _make_fake_goal(goal_id="g2", title="Second")]

        with patch("app.api.v1.goals.goal_service") as mock_svc:
            mock_svc.get_goals_by_user = AsyncMock(return_value=goals)
            response = await async_client.get("/api/v1/goals/", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["id"] == "g1"

    async def test_get_goal_detail_includes_tasks_and_progress(self, async_client, mock_db, make_fake_user):
        """GET /goals/{id} returns goal with tasks and progress."""
        fake_user = make_fake_user(user_id=1)
        mock_db.get_user.return_value = fake_user
        headers = {"Authorization": f"Bearer {_make_user_token(1)}"}
        fake_goal = _make_fake_goal()
        fake_tasks = [_make_fake_task(), _make_fake_task(task_id="t2", status=SDLCTaskStatus.CODING)]

        with patch("app.api.v1.goals.goal_service") as mock_svc:
            mock_svc.get_goal = AsyncMock(return_value=fake_goal)
            mock_svc.get_tasks_by_goal = AsyncMock(return_value=fake_tasks)
            mock_svc.get_progress = AsyncMock(
                return_value={"total": 2, "completed": 1, "active": 1, "cancelled": 0, "completion_ratio": 0.5}
            )
            response = await async_client.get("/api/v1/goals/goal-1", headers=headers)

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["id"] == "goal-1"
        assert len(data["tasks"]) == 2
        assert data["progress"]["total"] == 2
        assert data["progress"]["completed"] == 1

    async def test_update_goal_title_and_status(self, async_client, mock_db, make_fake_user):
        """PATCH /goals/{id} updates title and status."""
        fake_user = make_fake_user(user_id=1)
        mock_db.get_user.return_value = fake_user
        headers = {"Authorization": f"Bearer {_make_user_token(1)}"}
        original = _make_fake_goal()
        updated = _make_fake_goal(title="Updated Title", status=GoalStatus.COMPLETED)
        updated.completed_at = datetime.now(UTC)

        with patch("app.api.v1.goals.goal_service") as mock_svc:
            mock_svc.get_goal = AsyncMock(return_value=original)
            mock_svc.update_goal = AsyncMock(return_value=updated)
            response = await async_client.patch(
                "/api/v1/goals/goal-1",
                headers=headers,
                json={"title": "Updated Title", "status": "completed"},
            )

        assert response.status_code == 200, response.text
        assert response.json()["title"] == "Updated Title"
        assert response.json()["status"] == "completed"

    async def test_delete_goal_dissociates_tasks(self, async_client, mock_db, make_fake_user):
        """DELETE /goals/{id} dissociates all tasks before deleting."""
        fake_user = make_fake_user(user_id=1)
        mock_db.get_user.return_value = fake_user
        headers = {"Authorization": f"Bearer {_make_user_token(1)}"}
        fake_goal = _make_fake_goal()
        fake_tasks = [_make_fake_task(), _make_fake_task(task_id="t2")]

        with patch("app.api.v1.goals.goal_service") as mock_svc:
            mock_svc.get_goal = AsyncMock(return_value=fake_goal)
            mock_svc.get_tasks_by_goal = AsyncMock(return_value=fake_tasks)
            mock_svc.dissociate_task = AsyncMock()
            mock_svc.delete_goal = AsyncMock(return_value=True)
            response = await async_client.delete("/api/v1/goals/goal-1", headers=headers)

        assert response.status_code == 200, response.text
        assert mock_svc.dissociate_task.call_count == 2
        mock_svc.delete_goal.assert_called_once_with("goal-1")

    async def test_cannot_access_other_users_goal(self, async_client, mock_db, make_fake_user):
        """GET /goals/{id} returns 404 for another user's goal."""
        fake_user = make_fake_user(user_id=1)
        mock_db.get_user.return_value = fake_user
        headers = {"Authorization": f"Bearer {_make_user_token(1)}"}
        # Goal owned by user 2, not user 1
        other_goal = _make_fake_goal(user_id=2)

        with patch("app.api.v1.goals.goal_service") as mock_svc:
            mock_svc.get_goal = AsyncMock(return_value=other_goal)
            response = await async_client.get("/api/v1/goals/goal-1", headers=headers)

        assert response.status_code == 404


class TestGoalTaskAssociation:
    """Tests for task-to-goal association endpoints."""

    async def test_associate_task_to_goal(self, async_client, mock_db, make_fake_user):
        """POST /goals/{id}/tasks associates a task with the goal."""
        fake_user = make_fake_user(user_id=1)
        mock_db.get_user.return_value = fake_user
        headers = {"Authorization": f"Bearer {_make_user_token(1)}"}
        fake_goal = _make_fake_goal()
        fake_task = _make_fake_task()
        fake_tasks_after = [fake_task]

        with patch("app.api.v1.goals.goal_service") as mock_svc:
            mock_svc.get_goal = AsyncMock(return_value=fake_goal)
            mock_svc.associate_task = AsyncMock(return_value=fake_task)
            mock_svc.get_tasks_by_goal = AsyncMock(return_value=fake_tasks_after)
            mock_svc.get_progress = AsyncMock(
                return_value={"total": 1, "completed": 1, "active": 0, "cancelled": 0, "completion_ratio": 1.0}
            )
            response = await async_client.post(
                "/api/v1/goals/goal-1/tasks",
                headers=headers,
                json={"task_id": "task-1"},
            )

        assert response.status_code == 200, response.text
        mock_svc.associate_task.assert_called_once_with("goal-1", "task-1")
        assert len(response.json()["tasks"]) == 1

    async def test_remove_task_from_goal(self, async_client, mock_db, make_fake_user):
        """DELETE /goals/{id}/tasks/{tid} removes the association."""
        fake_user = make_fake_user(user_id=1)
        mock_db.get_user.return_value = fake_user
        headers = {"Authorization": f"Bearer {_make_user_token(1)}"}
        fake_goal = _make_fake_goal()

        with patch("app.api.v1.goals.goal_service") as mock_svc:
            mock_svc.get_goal = AsyncMock(return_value=fake_goal)
            mock_svc.dissociate_task = AsyncMock()
            response = await async_client.delete(
                "/api/v1/goals/goal-1/tasks/task-1",
                headers=headers,
            )

        assert response.status_code == 200, response.text
        mock_svc.dissociate_task.assert_called_once_with("task-1")
