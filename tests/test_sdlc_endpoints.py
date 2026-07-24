"""Integration tests for non-auth API endpoints.

Covers:
- GET / — root endpoint returns project info
- GET /health — health check (DB healthy / degraded)
- GET /api/v1/health — API router health check

These endpoints don't require auth, so they're a good smoke test for the
ASGI app wiring (middleware, routing, exception handlers).
"""

from unittest.mock import AsyncMock, patch


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
        with patch("app.api.v1.sdlc.orchestrator") as mock_orch:
            mock_orch.get_response = AsyncMock(return_value=fake_state)
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
        with patch("app.api.v1.sdlc.orchestrator") as mock_orch:
            mock_orch.get_response = AsyncMock(return_value=fake_state)
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
