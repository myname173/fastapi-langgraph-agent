"""Tests for workspace preview auth (P0-1).

The ``/workspace/{thread_id}/{path}`` endpoint replaced an unauthenticated
``StaticFiles`` mount. Every request must present a valid JWT (session or
user token) and the authenticated identity must own the requested
``thread_id``; otherwise the request is rejected. Files outside the extension
whitelist are never served.
"""

import pytest
from unittest.mock import AsyncMock, patch


def _token(sub: str, token_type: str = "session") -> str:
    """Build a JWT for ``sub`` (session id or user id).

    Imported lazily because ``app.utils.auth`` pulls in ``app.utils`` which has
    a circular import with ``app.schemas`` at module-load time; by the time a
    test runs, ``app`` is fully initialized and the import is safe.
    """
    from app.utils.auth import create_access_token

    return create_access_token(sub, token_type=token_type).access_token


@pytest.fixture
def workspace_file(temp_workspace, thread_id):
    """Write a sample artifact into the temp workspace for the given thread."""
    file_path = temp_workspace / thread_id / "index.html"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("<html><body>hello</body></html>", encoding="utf-8")
    return file_path


async def test_preview_requires_token(async_client, workspace_file, thread_id):
    """Unauthenticated requests are rejected with 401."""
    resp = await async_client.get(f"/workspace/{thread_id}/index.html")
    assert resp.status_code == 401


async def test_preview_invalid_token(async_client, workspace_file, thread_id):
    """Garbage tokens are rejected with 401."""
    resp = await async_client.get(f"/workspace/{thread_id}/index.html?token=garbage")
    assert resp.status_code == 401


async def test_preview_owner_session_succeeds(async_client, workspace_file, thread_id):
    """A session token whose sub == thread_id may read its own artifact."""
    token = _token(thread_id)
    resp = await async_client.get(f"/workspace/{thread_id}/index.html?token={token}")
    assert resp.status_code == 200
    assert "hello" in resp.text


async def test_preview_session_wrong_thread_forbidden(async_client, workspace_file, thread_id):
    """A session token for a different thread is forbidden (403)."""
    token = _token("another-thread-999")
    resp = await async_client.get(f"/workspace/{thread_id}/index.html?token={token}")
    assert resp.status_code == 403


async def test_preview_missing_file_404(async_client, temp_workspace, thread_id):
    """Missing artifacts return 404."""
    token = _token(thread_id)
    resp = await async_client.get(f"/workspace/{thread_id}/nope.html?token={token}")
    assert resp.status_code == 404


async def test_preview_disallowed_extension_forbidden(async_client, temp_workspace, thread_id):
    """Files outside the extension whitelist are never served (403)."""
    bad = temp_workspace / thread_id / "evil.exe"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("MZ", encoding="utf-8")

    token = _token(thread_id)
    resp = await async_client.get(f"/workspace/{thread_id}/evil.exe?token={token}")
    assert resp.status_code == 403


async def test_preview_user_token_owner_succeeds(async_client, workspace_file, thread_id):
    """A user token may read artifacts of a session it owns."""
    from app.models.session import Session
    from app.services.database import database_service

    fake_session = Session(id=thread_id, user_id=1, name="s", username="u")
    with patch.object(database_service, "get_session", AsyncMock(return_value=fake_session)):
        token = _token("1", token_type="user")
        resp = await async_client.get(f"/workspace/{thread_id}/index.html?token={token}")
    assert resp.status_code == 200


async def test_preview_user_token_non_owner_forbidden(async_client, workspace_file, thread_id):
    """A user token may NOT read artifacts of a session it does not own."""
    from app.models.session import Session
    from app.services.database import database_service

    fake_session = Session(id=thread_id, user_id=999, name="s", username="u")
    with patch.object(database_service, "get_session", AsyncMock(return_value=fake_session)):
        token = _token("1", token_type="user")
        resp = await async_client.get(f"/workspace/{thread_id}/index.html?token={token}")
    assert resp.status_code == 403
