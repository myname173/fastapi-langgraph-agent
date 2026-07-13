"""Test fixtures for SDLC Orchestrator tests.

Provides environment isolation (prevents loading ``.env.production``),
mock fixtures (DB / LLM / httpx client), and common test utilities.

Environment Isolation Strategy
------------------------------
``app.core.config`` executes ``load_env_file()`` at module import time,
which loads ``.env.{APP_ENV}`` if present.  On developer machines where
``APP_ENV=production`` is set (or ``.env`` is symlinked to
``.env.production``), this would leak real secrets (OPENAI_API_KEY,
JWT_SECRET_KEY, LANGFUSE keys, POSTGRES_PASSWORD) into the test process.

To prevent this, conftest.py sets ``APP_ENV=test`` *before* any
``app.*`` import, and patches critical ``settings`` attributes via an
autouse fixture as a second layer of defense.
"""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Layer 1: Force APP_ENV=test BEFORE any app import.
# This must happen before ``from app.core.config import settings`` so that
# ``config.load_env_file()`` looks for ``.env.test`` (not ``.env.production``).
# ---------------------------------------------------------------------------
os.environ["APP_ENV"] = "test"

# Layer 2 (defense in depth): Override critical secrets with test-only
# values via ``setdefault`` so we don't clobber values already set by the
# test runner, but ensure non-empty test values exist if .env.test is absent.
_TEST_JWT_SECRET = "test-secret-key-for-testing-only-not-for-production-use-32chars"
os.environ.setdefault("JWT_SECRET_KEY", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-key-not-real")
os.environ.setdefault("OPENAI_BASE_URL", "https://api.openai.com/v1")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "sk-lf-test")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")
os.environ.setdefault("POSTGRES_PASSWORD", "test-password")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")

# Now it's safe to import app modules.
# NOTE: Only import ``settings`` here — other app imports are deferred to
# fixture bodies to avoid triggering circular imports at module load time
# (e.g. ``app.utils.auth`` -> ``app.utils.__init__`` -> ``app.utils.graph``
# -> ``app.schemas`` -> ``app.schemas.graph`` -> ``app.utils.graph``).
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core.config import settings  # noqa: E402


# ---------------------------------------------------------------------------
# Pytest configuration hooks
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest — enforce test environment."""
    # Re-assert APP_ENV in case a prior plugin mutated it.
    os.environ["APP_ENV"] = "test"


@pytest.fixture(autouse=True)
def _enforce_test_settings() -> Iterator[None]:
    """Patch critical settings to test-only values for every test.

    Second layer of defense: even if ``.env.production`` was loaded by
    a prior import (e.g. via a conftest in a sibling package), this
    fixture guarantees tests use safe test values for JWT, API keys,
    and DB credentials.  Also disables optional subsystems that would
    attempt network I/O during tests.
    """
    overrides = {
        "JWT_SECRET_KEY": _TEST_JWT_SECRET,
        "JWT_ALGORITHM": "HS256",
        "OPENAI_API_KEY": "sk-test-key-not-real",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "LANGFUSE_SECRET_KEY": "sk-lf-test",
        "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
        "LANGFUSE_TRACING_ENABLED": False,
        "POSTGRES_PASSWORD": "test-password",
        "DEBUG": False,
        "ENABLE_ASYNC_DB": False,
        "ENABLE_MCP_CLIENT": False,
        "ENABLE_WEBSOCKET": False,
        "ENABLE_KNOWLEDGE_BASE": False,
    }
    with patch.multiple(settings, **overrides):
        yield


# ---------------------------------------------------------------------------
# Workspace & feature-flag fixtures (preserved from original conftest)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Iterator[Path]:
    """Create a temporary workspace root for testing.

    Patches ``settings.SDLC_WORKSPACE_ROOT`` to a temp directory.
    Automatically cleaned up by pytest's ``tmp_path``.
    """
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    with patch.object(settings, "SDLC_WORKSPACE_ROOT", workspace_root):
        yield workspace_root


@pytest.fixture
def enable_script_execution() -> Iterator[None]:
    """Enable script execution for tests."""
    with patch.object(settings, "ENABLE_SCRIPT_EXECUTION", True):
        yield


@pytest.fixture
def disable_script_execution() -> Iterator[None]:
    """Disable script execution for tests."""
    with patch.object(settings, "ENABLE_SCRIPT_EXECUTION", False):
        yield


@pytest.fixture
def enable_resource_quota() -> Iterator[None]:
    """Enable resource quota for tests."""
    with patch.object(settings, "ENABLE_RESOURCE_QUOTA", True):
        yield


@pytest.fixture
def disable_resource_quota() -> Iterator[None]:
    """Disable resource quota for tests."""
    with patch.object(settings, "ENABLE_RESOURCE_QUOTA", False):
        yield


@pytest.fixture
def enable_multimodal() -> Iterator[None]:
    """Enable multimodal for tests."""
    with patch.object(settings, "ENABLE_MULTIMODAL", True):
        yield


@pytest.fixture
def small_workspace_quota() -> Iterator[None]:
    """Set a very small workspace quota (1MB) for testing."""
    with patch.object(settings, "MAX_WORKSPACE_SIZE_MB", 1):
        yield


@pytest.fixture
def thread_id() -> str:
    """Standard thread ID for tests."""
    return "test-thread-001"


# ---------------------------------------------------------------------------
# Domain object fixtures — lightweight stand-ins for ORM models.
# ---------------------------------------------------------------------------


@pytest.fixture
def test_user_data() -> dict[str, Any]:
    """Return a dict of test user attributes (no DB persistence)."""
    return {
        "id": 1,
        "email": "test@example.com",
        "hashed_password": "$2b$12$testhashplaceholderforbcryptchecking",
        "username": "test_user",
    }


@pytest.fixture
def test_session_data() -> dict[str, Any]:
    """Return a dict of test session attributes (no DB persistence)."""
    return {
        "id": "test-session-001",
        "user_id": 1,
        "name": "Test Session",
        "username": "test_user",
    }


@pytest.fixture
def auth_headers(thread_id: str) -> dict[str, str]:
    """Return HTTP headers with a valid test JWT for ``thread_id``.

    Uses the test-only ``JWT_SECRET_KEY`` patched into ``settings`` by
    the autouse ``_enforce_test_settings`` fixture, so the token is
    verifiable by ``verify_token`` in test runs but useless elsewhere.
    """
    from app.utils.auth import create_access_token

    token = create_access_token(thread_id)
    return {"Authorization": f"Bearer {token.access_token}"}


@pytest.fixture
def expired_auth_headers(thread_id: str) -> dict[str, str]:
    """Return HTTP headers with an expired JWT (for negative tests)."""
    from app.utils.auth import create_access_token

    token = create_access_token(thread_id, expires_delta=timedelta(seconds=-1))
    return {"Authorization": f"Bearer {token.access_token}"}


# ---------------------------------------------------------------------------
# Mock DatabaseService fixture — replaces the module-level singleton in auth.py.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db() -> Iterator[MagicMock]:
    """Patch ``app.api.v1.auth.db_service`` with an AsyncMock-backed mock.

    All DB methods are ``AsyncMock`` instances (awaitable by default).
    Tests can configure return values per-test, e.g.::

        mock_db.get_user_by_email.return_value = None  # user not found
        mock_db.create_user.return_value = fake_user

    The fixture restores the original ``db_service`` after each test.
    """
    from app.api.v1 import auth as auth_module

    mock = MagicMock()
    # All public async methods on DatabaseService — return None / [] by default.
    mock.create_user = AsyncMock(return_value=None)
    mock.get_user = AsyncMock(return_value=None)
    mock.get_user_by_email = AsyncMock(return_value=None)
    mock.create_session = AsyncMock(return_value=None)
    mock.delete_session = AsyncMock(return_value=True)
    mock.get_session = AsyncMock(return_value=None)
    mock.get_user_sessions = AsyncMock(return_value=[])
    mock.update_session_name = AsyncMock(return_value=None)
    mock.delete_user_by_email = AsyncMock(return_value=True)
    mock.health_check = AsyncMock(return_value=True)

    with patch.object(auth_module, "db_service", mock):
        yield mock


# ---------------------------------------------------------------------------
# httpx AsyncClient fixture — uses ASGI transport, bypasses lifespan.
# ---------------------------------------------------------------------------


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    """Return an ``httpx.AsyncClient`` bound to the FastAPI app via ASGI.

    Uses ``ASGITransport`` so requests are routed in-process without
    binding a real TCP port.  ``root_path`` is set to the API prefix so
    tests can use relative paths like ``/api/v1/health``.

    The app's ``lifespan`` is NOT invoked — startup hooks (DB pool,
    cache, graph pre-warm, MCP) are skipped.  Tests that need DB
    behavior should use ``mock_db`` instead.
    """
    # Import here (not at module top) so that ``_enforce_test_settings``
    # has already patched ``settings`` before ``app.main`` is loaded.
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# LLM service mock fixture — for agent-node unit tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm_service() -> Iterator[MagicMock]:
    """Patch ``LLMService`` with an AsyncMock-backed mock.

    Returns a mock whose ``.call`` is an AsyncMock returning ``None``
    by default.  Agent-node tests should configure the return value to
    a Pydantic model (e.g. ``PlannerOutput``) appropriate to the node
    under test.
    """
    from app.services.llm.service import LLMService

    mock = MagicMock(spec=LLMService)
    mock.call = AsyncMock(return_value=None)
    mock.get_llm = MagicMock(return_value=MagicMock())
    mock.get_llm_for = MagicMock(return_value=MagicMock())

    # Patch the class so ``LLMService()`` returns our mock instance.
    with patch("app.services.llm.service.LLMService", return_value=mock):
        yield mock


# ---------------------------------------------------------------------------
# Utility: build a fake User / Session object for tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def make_fake_user():
    """Return a factory that builds User objects without DB persistence.

    Uses the normal constructor so SQLModel/SQLAlchemy internals
    (``_sa_instance_state``) are set correctly.  ``id`` is assigned
    after construction because it defaults to ``None``.
    """

    def _factory(
        user_id: int = 1,
        email: str = "test@example.com",
        username: str | None = "test_user",
        password: str = "TestPass123!",
    ) -> Any:
        from app.models.user import User

        user = User(
            email=email,
            hashed_password=User.hash_password(password),
            username=username,
        )
        user.id = user_id
        return user

    return _factory


@pytest.fixture
def make_fake_session():
    """Return a factory that builds Session objects without DB persistence."""

    def _factory(
        session_id: str = "test-session-001",
        user_id: int = 1,
        name: str = "Test Session",
        username: str | None = "test_user",
    ) -> Any:
        from app.models.session import Session

        session = Session(id=session_id, user_id=user_id, name=name, username=username)
        return session

    return _factory
