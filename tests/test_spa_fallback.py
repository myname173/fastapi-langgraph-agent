"""Tests for SPA history-API fallback (SPAStaticFiles).

Locks in the contract that unknown non-API paths serve index.html (so
history-API routes like /investor work on refresh), while API/docs paths
return a real 404 (not the SPA shell).
"""


class TestSpaFallback:
    """Tests for SPAStaticFiles fallback behavior."""

    async def test_unknown_path_serves_index_html(self, async_client):
        """Unknown frontend path (e.g. /investor) serves index.html, not 404."""
        response = await async_client.get("/investor")

        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert "<title>" in response.text

    async def test_unknown_api_path_returns_404_not_html(self, async_client):
        """Unknown API path returns JSON 404, not the SPA shell."""
        response = await async_client.get("/api/v1/does-not-exist")

        assert response.status_code == 404
        assert "text/html" not in response.headers.get("content-type", "").lower()

    async def test_docs_not_swallowed(self, async_client):
        """/docs (Swagger UI) is served normally, not swallowed by SPA fallback."""
        response = await async_client.get("/docs")

        assert response.status_code == 200
