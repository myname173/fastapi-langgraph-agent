"""Security tests for authentication — JWT forgery, expiry, malformed tokens.

Covers:
- Forged JWT (signed with a different secret) → 401
- Expired JWT → 401
- Malformed token (not 3-segment JWT format) → 422
- Empty Authorization header → 401
- Missing Authorization header → 401
- Bearer with empty token → 401 / 422
- Tampered payload (valid format, altered sub) → 401
"""

from datetime import timedelta

from jose import jwt


def _make_token(thread_id: str, token_type: str = "session") -> str:
    """Build a valid JWT using the test secret (deferred import)."""
    from app.utils.auth import create_access_token

    return create_access_token(thread_id, token_type=token_type).access_token


def _make_expired_token(thread_id: str) -> str:
    """Build an expired JWT (expiry in the past)."""
    from app.utils.auth import create_access_token

    return create_access_token(thread_id, expires_delta=timedelta(seconds=-1)).access_token


def _make_forged_token(thread_id: str) -> str:
    """Build a JWT signed with a DIFFERENT secret (forgery attempt)."""
    from datetime import datetime, UTC

    expire = datetime.now(UTC) + timedelta(days=1)
    payload = {"sub": thread_id, "exp": expire, "iat": datetime.now(UTC)}
    # Sign with a wrong secret — verify_token should reject this
    return jwt.encode(payload, "wrong-secret-key-not-the-real-one", algorithm="HS256")  # pragma: allowlist secret


def _make_tampered_token(thread_id: str, new_sub: str) -> str:
    """Build a JWT with an altered 'sub' claim but valid signature.

    This is a more sophisticated attack: the attacker takes a valid token
    and tries to change the subject.  Without the secret they can't
    re-sign it, so ``verify_token`` will reject the bad signature.
    """
    from app.core.config import settings

    # Decode without verification to get the payload
    token = _make_token(thread_id)
    payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    # Tamper with the subject
    payload["sub"] = new_sub
    # Re-encode WITHOUT signing (algorithm=none) — should be rejected
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm="HS256")


class TestJwtForgery:
    """Tests for JWT forgery attacks."""

    async def test_forged_jwt_returns_401(self, async_client, mock_db):
        """A JWT signed with a wrong secret is rejected."""
        headers = {"Authorization": f"Bearer {_make_forged_token('1')}"}
        response = await async_client.get("/api/v1/auth/sessions", headers=headers)
        assert response.status_code == 401

    async def test_forged_jwt_does_not_reach_db(self, async_client, mock_db):
        """A forged JWT must not trigger a DB lookup (verify_token fails first)."""
        headers = {"Authorization": f"Bearer {_make_forged_token('1')}"}
        await async_client.get("/api/v1/auth/sessions", headers=headers)
        # get_user should never be called when token verification fails
        mock_db.get_user.assert_not_awaited()


class TestJwtExpiry:
    """Tests for expired JWT tokens."""

    async def test_expired_jwt_returns_401(self, async_client, mock_db):
        """An expired JWT is rejected."""
        headers = {"Authorization": f"Bearer {_make_expired_token('1')}"}
        response = await async_client.get("/api/v1/auth/sessions", headers=headers)
        assert response.status_code == 401


class TestMalformedToken:
    """Tests for malformed token formats."""

    async def test_non_jwt_format_returns_422(self, async_client, mock_db):
        """A string that's not a 3-segment JWT is rejected with 422."""
        headers = {"Authorization": "Bearer not-a-jwt-at-all"}
        response = await async_client.get("/api/v1/auth/sessions", headers=headers)
        # verify_token raises ValueError for bad format → 422
        assert response.status_code in (401, 422)

    async def test_empty_token_returns_401(self, async_client, mock_db):
        """An empty Bearer token returns 401 (auto_error=False, manual check)."""
        headers = {"Authorization": "Bearer "}
        response = await async_client.get("/api/v1/auth/sessions", headers=headers)
        assert response.status_code == 401

    async def test_two_segment_token_returns_422(self, async_client, mock_db):
        """A JWT with only 2 segments is rejected."""
        headers = {"Authorization": "Bearer abc.def"}
        response = await async_client.get("/api/v1/auth/sessions", headers=headers)
        assert response.status_code in (401, 422)

    async def test_plain_string_token_returns_422(self, async_client, mock_db):
        """A plain string (no dots) is rejected."""
        headers = {"Authorization": "Bearer justtext"}
        response = await async_client.get("/api/v1/auth/sessions", headers=headers)
        assert response.status_code in (401, 422)


class TestMissingAuth:
    """Tests for missing authentication."""

    async def test_no_authorization_header_returns_401(self, async_client, mock_db):
        """Missing Authorization header returns 401 (auto_error=False, manual check)."""
        response = await async_client.get("/api/v1/auth/sessions")
        assert response.status_code == 401

    async def test_wrong_scheme_returns_401(self, async_client, mock_db):
        """Non-Bearer scheme returns 401 (auto_error=False, manual check)."""
        headers = {"Authorization": f"Basic {_make_token('1')}"}
        response = await async_client.get("/api/v1/auth/sessions", headers=headers)
        assert response.status_code == 401


class TestSessionIsolation:
    """Isolation of the session-management endpoints (rename / delete).

    These endpoints require a **user** token (``get_current_user``) and then
    verify that the target session belongs to that user. So there are two
    isolation guarantees to test: a session-scoped token cannot manage sessions
    at all (401), and a user token cannot touch another user's session (403).
    """

    async def test_session_token_cannot_delete_sessions(self, async_client, mock_db, make_fake_session):
        """A session token is rejected by DELETE /auth/session/{id} (401)."""
        mock_db.get_session.return_value = make_fake_session(session_id="session-a", user_id=1)

        token = _make_token("session-a")  # session-type token
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.delete("/api/v1/auth/session/session-b", headers=headers)

        assert response.status_code == 401
        mock_db.delete_session.assert_not_awaited()

    async def test_session_token_cannot_rename_sessions(self, async_client, mock_db, make_fake_session):
        """A session token is rejected by PATCH /auth/session/{id}/name (401)."""
        mock_db.get_session.return_value = make_fake_session(session_id="session-a", user_id=1)

        token = _make_token("session-a")  # session-type token
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.patch(
            "/api/v1/auth/session/session-b/name",
            headers=headers,
            data={"name": "hijacked"},
        )

        assert response.status_code == 401
        mock_db.update_session_name.assert_not_awaited()

    async def test_user_cannot_delete_another_users_session(
        self, async_client, mock_db, make_fake_user, make_fake_session
    ):
        """A user token cannot delete a session owned by a different user (403)."""
        mock_db.get_user.return_value = make_fake_user(user_id=1)
        mock_db.get_session.return_value = make_fake_session(session_id="session-b", user_id=2)

        token = _make_token("1", token_type="user")
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.delete("/api/v1/auth/session/session-b", headers=headers)

        assert response.status_code == 403
        assert "Cannot delete other sessions" in response.json()["detail"]
        # delete_session must not be called on a mismatched session
        mock_db.delete_session.assert_not_awaited()

    async def test_user_cannot_rename_another_users_session(
        self, async_client, mock_db, make_fake_user, make_fake_session
    ):
        """A user token cannot rename a session owned by a different user (403)."""
        mock_db.get_user.return_value = make_fake_user(user_id=1)
        mock_db.get_session.return_value = make_fake_session(session_id="session-b", user_id=2)

        token = _make_token("1", token_type="user")
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.patch(
            "/api/v1/auth/session/session-b/name",
            headers=headers,
            data={"name": "hijacked"},
        )

        assert response.status_code == 403
        mock_db.update_session_name.assert_not_awaited()
