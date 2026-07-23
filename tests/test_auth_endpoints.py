"""Integration tests for /api/v1/auth/* endpoints.

Covers:
- POST /auth/register — success, duplicate email, weak password
- POST /auth/login — success, wrong password, unknown user, bad grant_type
- POST /auth/session — success, invalid token
- PATCH /auth/session/{id}/name — success, mismatched session
- DELETE /auth/session/{id} — success, mismatched session
- GET /auth/sessions — success, empty list

Uses ``mock_db`` fixture to replace the module-level ``db_service`` and
``async_client`` fixture for in-process ASGI requests.  No real DB or
network I/O occurs.
"""


def _make_token(thread_id: str, token_type: str = "session") -> str:
    """Build a JWT token for tests (deferred import avoids circular import).

    ``app.utils.auth`` triggers ``app.utils.__init__`` → ``app.utils.graph``
    → ``app.schemas`` → ``app.schemas.graph`` → ``app.utils.graph`` (cycle).
    Importing inside the function breaks the cycle because by the time the
    test runs, all modules are fully loaded.
    """
    from app.utils.auth import create_access_token as _cat

    return _cat(thread_id, token_type=token_type).access_token


class TestRegisterEndpoint:
    """Tests for POST /api/v1/auth/register."""

    async def test_register_success(self, async_client, mock_db, make_fake_user):
        """Successful registration returns 200 with user info and token."""
        fake_user = make_fake_user(user_id=42, email="new@test.com", username="new_user")
        mock_db.get_user_by_email.return_value = None  # email not taken
        mock_db.create_user.return_value = fake_user

        response = await async_client.post(
            "/api/v1/auth/register",
            json={
                "email": "new@test.com",
                "password": "StrongPass123!",  # pragma: allowlist secret
                "username": "new_user",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "new@test.com"
        assert data["username"] == "new_user"
        assert "access_token" in data["token"]

    async def test_register_duplicate_email_returns_400(self, async_client, mock_db, make_fake_user):
        """Registering an existing email returns 400."""
        existing = make_fake_user(email="dup@test.com")
        mock_db.get_user_by_email.return_value = existing

        response = await async_client.post(
            "/api/v1/auth/register",
            json={"email": "dup@test.com", "password": "StrongPass123!"},  # pragma: allowlist secret
        )

        assert response.status_code == 400
        assert "already registered" in response.json()["detail"].lower()

    async def test_register_weak_password_returns_422(self, async_client, mock_db):
        """Password without uppercase/digit/special returns 422."""
        response = await async_client.post(
            "/api/v1/auth/register",
            json={"email": "weak@test.com", "password": "weakpass"},  # pragma: allowlist secret
        )

        assert response.status_code == 422

    async def test_register_invalid_email_returns_422(self, async_client, mock_db):
        """Malformed email returns 422."""
        response = await async_client.post(
            "/api/v1/auth/register",
            json={"email": "not-an-email", "password": "StrongPass123!"},  # pragma: allowlist secret
        )

        assert response.status_code == 422


class TestLoginEndpoint:
    """Tests for POST /api/v1/auth/login."""

    async def test_login_success(self, async_client, mock_db, make_fake_user):
        """Correct credentials return a token."""
        fake_user = make_fake_user(email="login@test.com", password="StrongPass123!")  # pragma: allowlist secret
        mock_db.get_user_by_email.return_value = fake_user

        response = await async_client.post(
            "/api/v1/auth/login",
            data={
                "email": "login@test.com",
                "password": "StrongPass123!",  # pragma: allowlist secret
                "grant_type": "password",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["token_type"] == "bearer"
        assert "access_token" in data
        assert "expires_at" in data

    async def test_login_wrong_password_returns_401(self, async_client, mock_db, make_fake_user):
        """Wrong password returns 401."""
        fake_user = make_fake_user(email="login@test.com", password="CorrectPass123!")  # pragma: allowlist secret
        mock_db.get_user_by_email.return_value = fake_user

        response = await async_client.post(
            "/api/v1/auth/login",
            data={
                "email": "login@test.com",
                "password": "WrongPass123!",
                "grant_type": "password",
            },  # pragma: allowlist secret
        )

        assert response.status_code == 401

    async def test_login_unknown_user_returns_401(self, async_client, mock_db):
        """Non-existent email returns 401."""
        mock_db.get_user_by_email.return_value = None

        response = await async_client.post(
            "/api/v1/auth/login",
            data={
                "email": "nobody@test.com",
                "password": "AnyPass123!",
                "grant_type": "password",
            },  # pragma: allowlist secret
        )

        assert response.status_code == 401

    async def test_login_bad_grant_type_returns_400(self, async_client, mock_db, make_fake_user):
        """Unsupported grant_type returns 400."""
        response = await async_client.post(
            "/api/v1/auth/login",
            data={
                "email": "x@test.com",
                "password": "AnyPass123!",
                "grant_type": "client_credentials",
            },  # pragma: allowlist secret
        )

        assert response.status_code == 400


class TestSessionCreateEndpoint:
    """Tests for POST /api/v1/auth/session."""

    async def test_create_session_success(self, async_client, mock_db, make_fake_user, make_fake_session):
        """Authenticated user can create a session."""
        fake_user = make_fake_user(user_id=7, email="sess@test.com")
        # Endpoint generates its own UUID session_id; we only control the name
        # via the db_service.create_session return value.
        fake_session = make_fake_session(session_id="ignored-by-endpoint", user_id=7, name="New Session")
        mock_db.get_user.return_value = fake_user
        mock_db.create_session.return_value = fake_session

        # Build a valid token for user_id=7
        token = _make_token("7", token_type="user")
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.post("/api/v1/auth/session", headers=headers)

        assert response.status_code == 200
        data = response.json()
        # session_id is a UUID generated by the endpoint, not from our mock
        assert "session_id" in data
        assert len(data["session_id"]) > 0
        assert data["name"] == "New Session"
        # Verify create_session was called with the user's username
        mock_db.create_session.assert_awaited_once()
        call_args = mock_db.create_session.call_args
        assert call_args.kwargs.get("username") == fake_user.username

    async def test_create_session_invalid_token_returns_401(self, async_client, mock_db):
        """Invalid token returns 401."""
        headers = {"Authorization": "Bearer invalid.token.here"}
        response = await async_client.post("/api/v1/auth/session", headers=headers)
        assert response.status_code == 401

    async def test_create_session_no_token_returns_401(self, async_client, mock_db):
        """Missing Authorization header returns 401 (auto_error=False, manual check)."""
        response = await async_client.post("/api/v1/auth/session")
        assert response.status_code == 401


class TestSessionNameUpdateEndpoint:
    """Tests for PATCH /api/v1/auth/session/{session_id}/name."""

    async def test_update_name_success(self, async_client, mock_db, make_fake_session):
        """Updating the matching session's name succeeds."""
        sess_id = "sess-to-update"
        fake_session = make_fake_session(session_id=sess_id, user_id=1, name="Old Name")
        updated_session = make_fake_session(session_id=sess_id, user_id=1, name="New Name")
        mock_db.get_session.return_value = fake_session
        mock_db.update_session_name.return_value = updated_session

        token = _make_token(sess_id)
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.patch(
            f"/api/v1/auth/session/{sess_id}/name",
            headers=headers,
            data={"name": "New Name"},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "New Name"

    async def test_update_name_mismatched_session_returns_403(self, async_client, mock_db, make_fake_session):
        """Updating a different session's name returns 403."""
        token_session = "my-session"
        target_session = "other-session"
        fake_session = make_fake_session(session_id=token_session, user_id=1)
        mock_db.get_session.return_value = fake_session

        token = _make_token(token_session)
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.patch(
            f"/api/v1/auth/session/{target_session}/name",
            headers=headers,
            data={"name": "New Name"},
        )

        assert response.status_code == 403


class TestSessionDeleteEndpoint:
    """Tests for DELETE /api/v1/auth/session/{session_id}."""

    async def test_delete_session_success(self, async_client, mock_db, make_fake_session):
        """Deleting the matching session succeeds."""
        sess_id = "sess-to-delete"
        fake_session = make_fake_session(session_id=sess_id, user_id=1)
        mock_db.get_session.return_value = fake_session
        mock_db.delete_session.return_value = True

        token = _make_token(sess_id)
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.delete(f"/api/v1/auth/session/{sess_id}", headers=headers)

        assert response.status_code == 200

    async def test_delete_session_mismatched_returns_403(self, async_client, mock_db, make_fake_session):
        """Deleting another session returns 403."""
        token_session = "my-session"
        target_session = "other-session"
        fake_session = make_fake_session(session_id=token_session, user_id=1)
        mock_db.get_session.return_value = fake_session

        token = _make_token(token_session)
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.delete(f"/api/v1/auth/session/{target_session}", headers=headers)

        assert response.status_code == 403


class TestGetUserSessionsEndpoint:
    """Tests for GET /api/v1/auth/sessions."""

    async def test_get_sessions_returns_list(self, async_client, mock_db, make_fake_user, make_fake_session):
        """Authenticated user gets a list of their sessions."""
        fake_user = make_fake_user(user_id=5, email="list@test.com")
        sessions = [
            make_fake_session(session_id="s1", user_id=5, name="First"),
            make_fake_session(session_id="s2", user_id=5, name="Second"),
        ]
        mock_db.get_user.return_value = fake_user
        mock_db.get_user_sessions.return_value = sessions

        token = _make_token("5", token_type="user")
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.get("/api/v1/auth/sessions", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["session_id"] == "s1"
        assert data[1]["session_id"] == "s2"

    async def test_get_sessions_empty_list(self, async_client, mock_db, make_fake_user):
        """User with no sessions gets an empty list."""
        fake_user = make_fake_user(user_id=6, email="empty@test.com")
        mock_db.get_user.return_value = fake_user
        mock_db.get_user_sessions.return_value = []

        token = _make_token("6", token_type="user")
        headers = {"Authorization": f"Bearer {token}"}

        response = await async_client.get("/api/v1/auth/sessions", headers=headers)

        assert response.status_code == 200
        assert response.json() == []

    async def test_get_sessions_no_token_returns_401(self, async_client, mock_db):
        """Missing Authorization header returns 401 (auto_error=False, manual check)."""
        response = await async_client.get("/api/v1/auth/sessions")
        assert response.status_code == 401
