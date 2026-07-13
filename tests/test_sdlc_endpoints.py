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
    """Tests for GET /."""

    async def test_root_returns_project_info(self, async_client):
        """Root endpoint returns name, version, and status."""
        response = await async_client.get("/")

        assert response.status_code == 200
        data = response.json()
        assert "name" in data
        assert "version" in data
        assert data["status"] == "healthy"
        assert "swagger_url" in data

    async def test_root_has_environment_field(self, async_client):
        """Root response includes the current environment."""
        response = await async_client.get("/")
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
