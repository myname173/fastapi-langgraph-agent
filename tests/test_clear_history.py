"""Tests for DELETE /messages scope semantics (session vs all).

Verifies that the ``scope`` query parameter controls whether long-term
memory is cleared alongside checkpoint history:
- ``scope=session`` (default): clears only checkpoints, backward compatible.
- ``scope=all``: forwards ``user_id`` so the agent can also clear mem0.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def fake_session(make_fake_session):
    """Build a fake session with a known ID for token matching."""
    return make_fake_session(session_id="s-clear-001", user_id=42)


@pytest.fixture
def auth_token(fake_session):
    """Mint a valid session-type JWT whose ``sub`` matches ``fake_session.id``."""
    # Import inside the fixture to avoid the app.utils -> app.schemas ->
    # app.utils circular import at module load time (see conftest.py note).
    from app.utils.auth import create_access_token

    token = create_access_token(fake_session.id)
    return token.access_token


class TestClearHistoryScope:
    """Verify scope=session clears only checkpoints; scope=all also clears memory."""

    async def test_scope_session_does_not_clear_memory(self, async_client, mock_db, fake_session, auth_token):
        """scope=session clears checkpoints but NOT long-term memory."""
        mock_db.get_session.return_value = fake_session

        with patch("app.api.v1.chatbot.agent") as mock_agent:
            mock_agent.clear_chat_history = AsyncMock()
            response = await async_client.delete(
                "/api/v1/chatbot/messages?scope=session",
                headers={"Authorization": f"Bearer {auth_token}"},
            )

        assert response.status_code == 200
        mock_agent.clear_chat_history.assert_awaited_once()
        kwargs = mock_agent.clear_chat_history.await_args.kwargs
        assert kwargs.get("scope") == "session"
        # user_id is always forwarded at the API layer; memory clearing is
        # gated inside agent.clear_chat_history (only when scope="all"), so
        # we do NOT assert on memory_service at this layer.

    async def test_scope_all_passes_user_id(self, async_client, mock_db, fake_session, auth_token):
        """scope=all forwards user_id so the agent can clear long-term memory."""
        mock_db.get_session.return_value = fake_session

        with patch("app.api.v1.chatbot.agent") as mock_agent:
            mock_agent.clear_chat_history = AsyncMock()
            response = await async_client.delete(
                "/api/v1/chatbot/messages?scope=all",
                headers={"Authorization": f"Bearer {auth_token}"},
            )

        assert response.status_code == 200
        mock_agent.clear_chat_history.assert_awaited_once()
        kwargs = mock_agent.clear_chat_history.await_args.kwargs
        assert kwargs.get("scope") == "all"
        assert kwargs.get("user_id") is not None
