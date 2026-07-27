"""Integration tests for non-auth API endpoints.

Covers:
- GET / — root endpoint returns project info
- GET /health — health check (DB healthy / degraded)
- GET /api/v1/health — API router health check

These endpoints don't require auth, so they're a good smoke test for the
ASGI app wiring (middleware, routing, exception handlers).
"""

from unittest.mock import AsyncMock, patch

from app.models.sdlc_task import SDLCTaskStatus


class TestRootEndpoint:
    """Tests for GET /api/info."""

    async def test_root_returns_project_info(self, async_client):
        """Root endpoint returns name, version, and status."""
        response = await async_client.get("/api/info")

        assert response.status_code == 200
        data = response.json()
        assert "name" in data
        assert "version" in data
        assert data["status"] == "healthy"
        assert "swagger_url" in data

    async def test_root_has_environment_field(self, async_client):
        """Root response includes the current environment."""
        response = await async_client.get("/api/info")
        data = response.json()
        assert "environment" in data
        # Tests run with APP_ENV=test → Environment.TEST
        assert data["environment"] in ("test", "development")


class TestHealthEndpoint:
    """Tests for GET /health."""

    async def test_health_healthy_when_db_ok(self, async_client):
        """When DB health check passes, /health returns 200."""
        with patch("app.main.database_service") as mock_db:
            mock_db.health_check = AsyncMock(return_value=True)
            response = await async_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["components"]["database"] == "healthy"

    async def test_health_degraded_when_db_fails(self, async_client):
        """When DB health check fails, /health returns 503."""
        with patch("app.main.database_service") as mock_db:
            mock_db.health_check = AsyncMock(return_value=False)
            response = await async_client.get("/health")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["components"]["database"] == "unhealthy"


class TestApiHealthEndpoint:
    """Tests for GET /api/v1/health (lightweight, no DB check)."""

    async def test_api_health_returns_200(self, async_client):
        """API health endpoint always returns 200."""
        response = await async_client.get("/api/v1/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data


# ---------------------------------------------------------------------------
# SDLC coder model selection feature (GET /sdlc/models + coder_model validation)
# ---------------------------------------------------------------------------


class TestSDLCModelSelection:
    """Tests for the SDLC coder model selection feature.

    Covers:
    - GET /api/v1/sdlc/models returns the eligible list + router default
    - POST /api/v1/sdlc/ rejects an invalid coder_model with 400
    - POST /api/v1/sdlc/ accepts None coder_model (back-compat) and passes it through
    - POST /api/v1/sdlc/ accepts an eligible coder_model and passes it through
    - POST /api/v1/sdlc/stream rejects an invalid coder_model with 400
    """

    def _auth_setup(self, mock_db, make_fake_session, thread_id):
        """Configure mock_db so get_current_session succeeds for thread_id.

        get_current_session extracts session_id from the JWT (== thread_id
        from the auth_headers fixture) and calls db_service.get_session().
        We return a fake Session with matching id so the dependency resolves.
        """
        fake_session = make_fake_session(session_id=thread_id, user_id=1)
        mock_db.get_session.return_value = fake_session
        return fake_session

    async def test_get_models_returns_eligible_list_and_default(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """GET /sdlc/models returns settings.CODER_ELIGIBLE_MODELS + router default."""
        self._auth_setup(mock_db, make_fake_session, thread_id)

        from app.core.config import settings
        from app.services.llm.router import model_router

        response = await async_client.get("/api/v1/sdlc/models", headers=auth_headers)

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["models"] == list(settings.CODER_ELIGIBLE_MODELS)
        assert data["default"] == model_router.get_model_for("coder")

    async def test_post_sdlc_rejects_invalid_coder_model(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """POST /sdlc/ with an invalid coder_model returns 400 and never calls the orchestrator."""
        self._auth_setup(mock_db, make_fake_session, thread_id)

        with patch("app.api.v1.sdlc.orchestrator") as mock_orch:
            response = await async_client.post(
                "/api/v1/sdlc/",
                headers=auth_headers,
                json={"requirements": "test", "coder_model": "definitely-not-a-model"},
            )

        assert response.status_code == 400, response.text
        assert "coder_model" in response.json()["detail"]
        mock_orch.get_response.assert_not_called()

    async def test_post_sdlc_accepts_none_coder_model(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """POST /sdlc/ without coder_model calls orchestrator with coder_model=None (back-compat)."""
        self._auth_setup(mock_db, make_fake_session, thread_id)

        # Minimal valid SDLCState response so SDLCResponse construction succeeds.
        fake_state = {
            "messages": [],
            "status": "planning",
            "plan": [],
            "code_changes": [],
            "test_results": [],
            "feedback": [],
            "security_issues": [],
            "is_complete": False,
            "human_approval_needed": False,
        }
        with (
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task,
        ):
            mock_orch.get_response = AsyncMock(return_value=fake_state)
            # create_task is now wired into POST /; mock it so the test
            # does not hit the (unconfigured) DB via sdlc_task_service.
            mock_task.create_task = AsyncMock(return_value=None)
            mock_task.update_task_status = AsyncMock()
            mock_task.update_task_plan = AsyncMock()
            response = await async_client.post(
                "/api/v1/sdlc/",
                headers=auth_headers,
                json={"requirements": "test"},
            )

        assert response.status_code == 200, response.text
        # coder_model=None must be passed through (back-compat with old clients).
        _, kwargs = mock_orch.get_response.call_args
        assert kwargs.get("coder_model") is None

    async def test_post_sdlc_accepts_eligible_coder_model(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """POST /sdlc/ with an eligible coder_model passes it through to the orchestrator."""
        self._auth_setup(mock_db, make_fake_session, thread_id)

        from app.core.config import settings

        # Pick the first eligible model that isn't the default, to prove the
        # value is actually propagated (not just defaulted).
        chosen = settings.CODER_ELIGIBLE_MODELS[0]

        fake_state = {
            "messages": [],
            "status": "planning",
            "plan": [],
            "code_changes": [],
            "test_results": [],
            "feedback": [],
            "security_issues": [],
            "is_complete": False,
            "human_approval_needed": False,
        }
        with (
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task,
        ):
            mock_orch.get_response = AsyncMock(return_value=fake_state)
            mock_task.create_task = AsyncMock(return_value=None)
            mock_task.update_task_status = AsyncMock()
            mock_task.update_task_plan = AsyncMock()
            response = await async_client.post(
                "/api/v1/sdlc/",
                headers=auth_headers,
                json={"requirements": "test", "coder_model": chosen},
            )

        assert response.status_code == 200, response.text
        _, kwargs = mock_orch.get_response.call_args
        assert kwargs.get("coder_model") == chosen

    async def test_post_sdlc_stream_rejects_invalid_coder_model(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """POST /sdlc/stream with an invalid coder_model returns 400 before streaming starts."""
        self._auth_setup(mock_db, make_fake_session, thread_id)

        with patch("app.api.v1.sdlc.orchestrator") as mock_orch:
            response = await async_client.post(
                "/api/v1/sdlc/stream",
                headers=auth_headers,
                json={"requirements": "test", "coder_model": "definitely-not-a-model"},
            )

        assert response.status_code == 400, response.text
        assert "coder_model" in response.json()["detail"]
        mock_orch.stream_events.assert_not_called()


class TestSDLCTaskPersistence:
    """Tests for SDLCTask write-path wiring (previously dead code).

    Covers the 4 lifecycle hooks: POST /, POST /stream, approve, reject.
    Verifies create_task is called, status is mirrored, quota overflow
    surfaces as 429, and persistence failures are best-effort.
    """

    def _auth_setup(self, mock_db, make_fake_session, thread_id):
        """Configure mock_db so get_current_session succeeds for thread_id."""
        fake_session = make_fake_session(session_id=thread_id, user_id=1)
        mock_db.get_session.return_value = fake_session
        return fake_session

    @staticmethod
    def _fake_state(status: str = "completed", is_complete: bool = True, plan: list | None = None) -> dict:
        """Minimal valid SDLCState dict for SDLCResponse construction."""
        return {
            "messages": [],
            "status": status,
            "plan": plan if plan is not None else [],
            "code_changes": [],
            "test_results": [],
            "feedback": [],
            "security_issues": [],
            "is_complete": is_complete,
            "human_approval_needed": False,
        }

    async def test_create_task_called_on_orchestrate(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """POST /sdlc/ creates an SDLCTask row and mirrors completion status + plan."""
        self._auth_setup(mock_db, make_fake_session, thread_id)
        fake_state = self._fake_state(
            status="completed", is_complete=True, plan=[{"id": "step-1", "description": "test step"}]
        )

        with (
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task,
        ):
            mock_orch.get_response = AsyncMock(return_value=fake_state)
            mock_task.create_task = AsyncMock(return_value=None)
            mock_task.update_task_status = AsyncMock()
            mock_task.update_task_plan = AsyncMock()
            response = await async_client.post("/api/v1/sdlc/", headers=auth_headers, json={"requirements": "test"})

        assert response.status_code == 200, response.text
        # create_task called with task_id == thread_id (uuid generated internally),
        # matching the approval_log convention.
        _, kwargs = mock_task.create_task.call_args
        assert kwargs["task_id"] == kwargs["thread_id"]
        assert kwargs["user_id"] == 1
        assert kwargs["requirements"] == "test"
        # Status mirrored to COMPLETED and plan persisted (best-effort).
        task_id = kwargs["task_id"]
        mock_task.update_task_status.assert_awaited_once_with(task_id, SDLCTaskStatus("completed"))
        mock_task.update_task_plan.assert_awaited_once()

    async def test_quota_exceeded_returns_429(self, async_client, auth_headers, mock_db, make_fake_session, thread_id):
        """create_task quota overflow surfaces as 429 (not 500) and skips the workflow."""
        self._auth_setup(mock_db, make_fake_session, thread_id)

        with (
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task,
        ):
            mock_task.create_task = AsyncMock(
                side_effect=ValueError("User 1 has reached the maximum of 20 active tasks")
            )
            response = await async_client.post("/api/v1/sdlc/", headers=auth_headers, json={"requirements": "test"})

        assert response.status_code == 429, response.text
        assert "maximum of 20 active tasks" in response.json()["detail"]
        # Workflow must NOT run when the task row could not be created.
        mock_orch.get_response.assert_not_called()

    async def test_stream_creates_and_persists_task(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """POST /sdlc/stream creates a task at start and mirrors final state at end."""
        self._auth_setup(mock_db, make_fake_session, thread_id)
        fake_state = self._fake_state(status="completed", is_complete=True)

        async def _fake_stream(*args, **kwargs):
            yield {"event": "on_chain_end", "data": {}}

        with (
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task,
        ):
            mock_orch.stream_events = _fake_stream
            mock_orch.get_state = AsyncMock(return_value=fake_state)
            mock_task.create_task = AsyncMock(return_value=None)
            mock_task.update_task_status = AsyncMock()
            mock_task.update_task_plan = AsyncMock()
            response = await async_client.post(
                "/api/v1/sdlc/stream", headers=auth_headers, json={"requirements": "test"}
            )

        assert response.status_code == 200, response.text
        mock_task.create_task.assert_awaited_once()
        _, kwargs = mock_task.create_task.call_args
        assert kwargs["task_id"] == kwargs["thread_id"]
        # Final checkpoint state mirrored via get_state -> update_task_status.
        mock_task.update_task_status.assert_awaited_once_with(kwargs["task_id"], SDLCTaskStatus("completed"))

    async def test_persist_failure_does_not_break_request(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """If update_task_status raises, the POST / response still succeeds (best-effort)."""
        self._auth_setup(mock_db, make_fake_session, thread_id)
        fake_state = self._fake_state(status="completed", is_complete=True)

        with (
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task,
        ):
            mock_orch.get_response = AsyncMock(return_value=fake_state)
            mock_task.create_task = AsyncMock(return_value=None)
            mock_task.update_task_status = AsyncMock(side_effect=RuntimeError("db down"))
            mock_task.update_task_plan = AsyncMock()
            response = await async_client.post("/api/v1/sdlc/", headers=auth_headers, json={"requirements": "test"})

        assert response.status_code == 200, response.text

    async def test_approve_mirrors_status(self, async_client, auth_headers, mock_db, make_fake_session, thread_id):
        """POST /{thread_id}/approve mirrors the resumed status into SDLCTask."""
        self._auth_setup(mock_db, make_fake_session, thread_id)
        fake_state = self._fake_state(status="completed", is_complete=True)

        with (
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task,
            patch("app.api.v1.sdlc.approval_log_service") as mock_log,
        ):
            mock_orch.approve_task = AsyncMock(return_value=fake_state)
            mock_task.update_task_status = AsyncMock()
            mock_task.update_task_plan = AsyncMock()
            mock_log.log_approval = AsyncMock()
            response = await async_client.post("/api/v1/sdlc/%s/approve" % thread_id, headers=auth_headers)

        assert response.status_code == 200, response.text
        mock_task.update_task_status.assert_awaited_once_with(thread_id, SDLCTaskStatus("completed"))


class TestSDLCGoalContext:
    """Tests for Goal context injection into SDLC workflow."""

    def _auth_setup(self, mock_db, make_fake_session, thread_id):
        """Configure mock_db so get_current_session succeeds for thread_id."""
        fake_session = make_fake_session(session_id=thread_id, user_id=1)
        mock_db.get_session.return_value = fake_session
        return fake_session

    @staticmethod
    def _fake_state(status: str = "completed", is_complete: bool = True, plan: list | None = None) -> dict:
        """Minimal valid SDLCState dict for SDLCResponse construction."""
        return {
            "messages": [],
            "status": status,
            "plan": plan if plan is not None else [],
            "code_changes": [],
            "test_results": [],
            "feedback": [],
            "security_issues": [],
            "is_complete": is_complete,
            "human_approval_needed": False,
        }

    async def test_sdlc_with_goal_id_injects_context(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """POST /sdlc/ with goal_id injects historical context into project_context."""
        self._auth_setup(mock_db, make_fake_session, thread_id)
        fake_state = self._fake_state()

        with (
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task,
            patch("app.api.v1.sdlc.goal_service") as mock_goal,
        ):
            mock_orch.get_response = AsyncMock(return_value=fake_state)
            mock_task.create_task = AsyncMock(return_value=None)
            mock_task.update_task_status = AsyncMock()
            mock_task.update_task_plan = AsyncMock()
            mock_goal.build_context = AsyncMock(return_value="# Goal: Test\n\nHistorical context here")

            response = await async_client.post(
                "/api/v1/sdlc/",
                headers=auth_headers,
                json={"requirements": "new feature", "goal_id": "goal-123"},
            )

        assert response.status_code == 200, response.text
        mock_goal.build_context.assert_called_once_with("goal-123", 1)
        call_kwargs = mock_orch.get_response.call_args.kwargs
        assert "Historical context here" in call_kwargs["project_context"]
        # create_task should also receive goal_id
        mock_task.create_task.assert_called_once()
        assert mock_task.create_task.call_args.kwargs.get("goal_id") == "goal-123"

    async def test_sdlc_without_goal_id_backward_compatible(
        self, async_client, auth_headers, mock_db, make_fake_session, thread_id
    ):
        """POST /sdlc/ without goal_id does not call build_context (backward compat)."""
        self._auth_setup(mock_db, make_fake_session, thread_id)
        fake_state = self._fake_state()

        with (
            patch("app.api.v1.sdlc.orchestrator") as mock_orch,
            patch("app.api.v1.sdlc.sdlc_task_service") as mock_task,
            patch("app.api.v1.sdlc.goal_service") as mock_goal,
        ):
            mock_orch.get_response = AsyncMock(return_value=fake_state)
            mock_task.create_task = AsyncMock(return_value=None)
            mock_task.update_task_status = AsyncMock()
            mock_task.update_task_plan = AsyncMock()

            response = await async_client.post(
                "/api/v1/sdlc/",
                headers=auth_headers,
                json={"requirements": "simple task"},
            )

        assert response.status_code == 200, response.text
        mock_goal.build_context.assert_not_called()
        call_kwargs = mock_orch.get_response.call_args.kwargs
        assert call_kwargs["project_context"] is None
