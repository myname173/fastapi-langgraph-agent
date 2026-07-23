"""Integration tests for the MCP client wiring (Phase 3).

Covers three layers:

1. **Unit tests** (no network) — verify ``settings.MCP_DEFAULT_TRANSPORT``
   default value, ``stdio`` graceful skip, and ``MultiServerMCPClient``
   call shape via mocking.

2. **Connectivity tests** (require the in-repo MCP server running on
   ``localhost:8002``) — verify the full
   ``register_mcp_server → MultiServerMCPClient → server`` chain against
   the streamable-http transport, plus graceful failure for the legacy
   SSE transport against a streamable-http-only server.

The connectivity tests auto-skip when the MCP server is unreachable,
so this file is safe to run in CI without a container sidecar.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.langgraph.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp_server_reachable(host: str = "localhost", port: int = 8002) -> bool:
    """Probe whether the MCP server container is accepting connections."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


# Shared skip guard for connectivity tests — avoids repeating the socket
# probe in every test function. ``pytest.mark.connectivity`` lets operators
# deselect these with ``-m "not connectivity"``.
mcp_required = pytest.mark.connectivity
mcp_skip_if_down = pytest.mark.skipif(
    not _mcp_server_reachable(),
    reason="MCP server not reachable on localhost:8002 (run: docker compose up -d mcp)",
)


# ---------------------------------------------------------------------------
# Unit tests — no network, no MCP packages required.
# ---------------------------------------------------------------------------


def test_mcp_default_transport_setting_default() -> None:
    """``MCP_DEFAULT_TRANSPORT`` should default to ``streamable_http``.

    This guards against accidental env-file drift: if a future change
    silently flips the default to ``sse`` (deprecated), this test fails
    fast before deployment.
    """
    assert settings.MCP_DEFAULT_TRANSPORT == "streamable_http", (
        f"expected 'streamable_http', got {settings.MCP_DEFAULT_TRANSPORT!r}"
    )


@pytest.mark.asyncio
async def test_register_mcp_server_stdio_graceful_skip() -> None:
    """``transport='stdio'`` via URL entry point must return [] (no raise).

    stdio requires ``command``/``args`` fields rather than a URL, so the
    URL-based ``register_mcp_server`` gracefully skips instead of
    attempting a connection that would hang or error.
    """
    registry = ToolRegistry()
    loaded = await registry.register_mcp_server("http://localhost:8002/mcp", transport="stdio")
    assert loaded == []


@pytest.mark.asyncio
async def test_register_mcp_server_uses_settings_default_transport(monkeypatch) -> None:
    """When ``transport=None``, the method reads from ``settings.MCP_DEFAULT_TRANSPORT``.

    Uses a mock to avoid a real network call — verifies the configuration
    plumbing, not the wire protocol.
    """
    # Force the settings default so the test is hermetic against .env drift.
    monkeypatch.setattr(settings, "MCP_DEFAULT_TRANSPORT", "streamable_http")

    # Mock the langchain_mcp_adapters client at import site.
    mock_client = MagicMock()
    mock_tool = MagicMock()
    mock_tool.name = "fake_tool"
    mock_client.get_tools = AsyncMock(return_value=[mock_tool])

    with patch(
        "langchain_mcp_adapters.client.MultiServerMCPClient",
        return_value=mock_client,
    ) as mock_cls:
        registry = ToolRegistry()
        loaded = await registry.register_mcp_server("http://example:8002/mcp")

    # Confirm the client was constructed with the default transport.
    mock_cls.assert_called_once_with(
        {
            "mcp_server": {
                "url": "http://example:8002/mcp",
                "transport": "streamable_http",
            }
        }
    )
    assert loaded == ["mcp_fake_tool"]


@pytest.mark.asyncio
async def test_register_mcp_server_import_error_is_non_fatal(monkeypatch) -> None:
    """Missing ``mcp`` packages must log + return [], not raise.

    This is the critical non-fatal degradation guarantee — the app must
    still boot if MCP deps are absent (opt-in feature).
    """
    # Simulate ImportError on the lazy import inside register_mcp_server.
    import builtins

    real_import = builtins.__import__

    def _fail_on_mcp(name, *args, **kwargs):
        if name.startswith("langchain_mcp_adapters"):
            raise ImportError("simulated missing package")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_on_mcp)

    registry = ToolRegistry()
    loaded = await registry.register_mcp_server("http://localhost:8002/mcp")
    assert loaded == []


# ---------------------------------------------------------------------------
# Connectivity tests — require the in-repo MCP server running on localhost:8002.
# Auto-skip when unreachable so this file is CI-safe.
# ---------------------------------------------------------------------------


@mcp_required
@mcp_skip_if_down
@pytest.mark.asyncio
async def test_register_mcp_server_streamable_http_loads_tools() -> None:
    """Full chain: streamable_http transport loads all 9 in-repo tools.

    This is the canonical Phase 1+3 integration check — if it fails,
    either the server is broken or the registry wiring regressed.
    """
    pytest.importorskip("langchain_mcp_adapters")

    registry = ToolRegistry()
    loaded = await registry.register_mcp_server("http://localhost:8002/mcp")

    expected = {
        "mcp_git_log",
        "mcp_git_show",
        "mcp_git_branch_list",
        "mcp_git_status_short",
        "mcp_git_diff_stat",
        "mcp_query_tasks",
        "mcp_query_task_by_id",
        "mcp_query_task_stats",
        "mcp_query_eval_golden",
    }
    assert set(loaded) == expected, f"got {sorted(loaded)}"

    # coder + tester agents should see all MCP tools (agents={"coder","tester"}).
    coder_mcp = [t for t in registry.get_for_agent("coder") if t.name.startswith("mcp_")]
    tester_mcp = [t for t in registry.get_for_agent("tester") if t.name.startswith("mcp_")]
    assert len(coder_mcp) == 9
    assert len(tester_mcp) == 9


@mcp_required
@mcp_skip_if_down
@pytest.mark.asyncio
async def test_register_mcp_server_sse_graceful_failure() -> None:
    """Explicit ``transport='sse'`` against a streamable-http server returns [].

    The in-repo server only speaks streamable-http, so SSE handshake 404s.
    The client must translate that into a graceful empty list (logged as
    a warning), not propagate an exception that would abort app startup.
    """
    pytest.importorskip("langchain_mcp_adapters")

    registry = ToolRegistry()
    loaded = await registry.register_mcp_server("http://localhost:8002/sse", transport="sse")
    assert loaded == []


@mcp_required
@mcp_skip_if_down
@pytest.mark.asyncio
async def test_mcp_tool_invocation_end_to_end() -> None:
    """Smoke-test a single tool invocation through the loaded MCP tool.

    Calls ``query_task_stats`` (deterministic seed data: 2 completed,
    1 cancelled) to confirm the round-trip works end-to-end.
    """
    pytest.importorskip("langchain_mcp_adapters")

    registry = ToolRegistry()
    await registry.register_mcp_server("http://localhost:8002/mcp")

    stats_tool = registry.get_by_name("mcp_query_task_stats")
    result = await stats_tool.ainvoke({})

    # Result shape: [{'type': 'text', 'text': '{"completed": 2, ...}'}]
    text_payload = result[0]["text"] if isinstance(result, list) else str(result)
    assert "completed" in text_payload, f"unexpected result: {result!r}"
    assert "cancelled" in text_payload, f"unexpected result: {result!r}"
