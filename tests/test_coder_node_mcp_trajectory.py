"""Trajectory-level eval assertions for coder_node MCP tool integration.

Verifies that the coder node, when given an LLM response containing an
``mcp_query_tasks`` (or other ``mcp_*``) tool_call, correctly:

  1. Invokes the registered MCP tool with the LLM-provided args.
  2. Increments the ``mcp_tool_calls_total{tool, status}`` Prometheus counter.
  3. Returns a ``Command(goto="supervisor")`` with a ``ToolMessage`` whose
     content contains the MCP tool's return value.
  4. Surfaces a friendly error ``ToolMessage`` + ``failed``-status metric
     increment when the MCP tool raises (graceful degradation).

CI-safe: no real LLM, no real MCP server, no docker dependency, no
prometheus port. The MCP tool is mocked via ``MagicMock`` with
``ainvoke = AsyncMock(...)``; the LLM is mocked via a fully mocked
``ctx.llm_service.get_llm_for(...).bind_tools(...).ainvoke(...)`` chain.
``prometheus_client.REGISTRY`` is sampled in-memory to assert counter
deltas — no real Prometheus scrape needed.

Test scope: this is a *trajectory-level* eval (per the MCP integration
gap analysis) — it proves the coder agent actually *uses* MCP tools
when the LLM emits a tool_call, not just that the tools are registered
(which is covered by ``tests/test_mcp_integration.py``).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph.state import Command
from prometheus_client import REGISTRY

from app.core.langgraph.agents.context import AgentContext
from app.core.langgraph.agents.coder_node import make_coder_node


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp_tool(name: str, return_value: Any = None, exc: Exception | None = None) -> MagicMock:
    """Build a fake LangChain tool with the given name and ainvoke behavior.

    Mimics what ``registry.register_mcp_server`` produces: a BaseTool-shaped
    object whose ``.name`` starts with ``mcp_`` and whose ``.ainvoke`` is
    awaitable.

    Args:
        name: Tool name (must start with ``mcp_``).
        return_value: Value returned by ``ainvoke`` on success path.
        exc: Exception raised by ``ainvoke`` on failure path (mutually
            exclusive with ``return_value``).

    Returns:
        MagicMock shaped like a LangChain BaseTool.
    """
    tool = MagicMock()
    tool.name = name
    if exc is not None:
        tool.ainvoke = AsyncMock(side_effect=exc)
    else:
        tool.ainvoke = AsyncMock(return_value=return_value)
    return tool


def _build_ctx_with_mcp_tool(
    mcp_tools: list[MagicMock],
    *,
    llm_response: Any,
) -> AgentContext:
    """Build an AgentContext whose registry returns the given MCP tools for "coder".

    The mock LLM service's
    ``get_llm_for(...).bind_tools(...).ainvoke(...)`` chain returns
    ``llm_response`` (an AIMessage with tool_calls).

    Args:
        mcp_tools: List of mocked tools returned by ``registry.get_for_agent``.
        llm_response: AIMessage returned by the mocked LLM.

    Returns:
        AgentContext with fully mocked services.
    """
    # Mock the bind_tools → ainvoke chain.
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=llm_response)
    mock_llm = MagicMock()
    mock_llm.bind_tools = MagicMock(return_value=bound)

    mock_llm_service = MagicMock()
    mock_llm_service.get_llm_for = MagicMock(return_value=mock_llm)

    mock_registry = MagicMock()
    # coder_node calls registry.get_for_agent("coder") to build file_tools.
    mock_registry.get_for_agent = MagicMock(return_value=mcp_tools)
    # _apply_smart_edits_and_validate may call get_by_name — return a mock
    # so it doesn't raise KeyError. Only invoked if parsed.edits exists,
    # but defensive mock avoids breakage on future refactors.
    mock_registry.get_by_name = MagicMock(return_value=MagicMock(ainvoke=AsyncMock(return_value={})))

    mock_router = MagicMock()
    mock_router.get_model_for = MagicMock(return_value="test-model")
    mock_router.get_model_for_call = MagicMock(return_value=None)

    return AgentContext(
        llm_service=mock_llm_service,
        registry=mock_registry,
        code_index_service=MagicMock(),
        smart_search_service=MagicMock(),
        knowledge_base_service=MagicMock(),
        model_router=mock_router,
    )


def _base_state() -> dict[str, Any]:
    """Minimal SDLCState for coder input."""
    return {
        "messages": [{"role": "user", "content": "list recent tasks"}],
        "requirements": "list recent tasks",
        "plan": [],
        "code_changes": [],
        "coder_retry_count": 0,
    }


def _mcp_tool_calls_metric_value(tool: str, status: str) -> float:
    """Read ``mcp_tool_calls_total{tool, status}`` from in-memory REGISTRY.

    Args:
        tool: Tool name label (e.g. ``"mcp_query_tasks"``).
        status: Status label (``"success"`` or ``"failed"``).

    Returns:
        Current counter value (0.0 if the label combination has never
        been incremented — prometheus_client semantics).

    Note:
        prometheus_client strips the ``_total`` suffix from the Counter
        family name (``Counter("mcp_tool_calls_total")`` → family name
        ``"mcp_tool_calls"``), but the sample name keeps it
        (``"mcp_tool_calls_total"``). We match on sample name for
        robustness.
    """
    total = 0.0
    for metric_family in REGISTRY.collect():
        for sample in metric_family.samples:
            if (
                sample.name == "mcp_tool_calls_total"
                and sample.labels.get("tool") == tool
                and sample.labels.get("status") == status
            ):
                total += sample.value
    return total


# Common patch context manager for coder_node internals that touch real
# services. _build_code_index_context is coder_node-specific (reviewer/
# planner don't call it) — must be patched or MagicMock code_index_service
# returns would cascade into unexpected branches.
_PCODER_PATCHES = (
    patch("app.core.langgraph.agents.coder_node.load_agent_prompt", return_value="sys"),
    patch("app.core.langgraph.agents.coder_node.prepare_sdlc_messages", return_value=[]),
    patch(
        "app.core.langgraph.agents.coder_node._build_code_index_context",
        AsyncMock(return_value=""),
    ),
)


# ---------------------------------------------------------------------------
# Happy path: single MCP tool_call
# ---------------------------------------------------------------------------


class TestCoderNodeMcpTrajectoryHappyPath:
    """Verify successful MCP tool invocation path."""

    async def test_mcp_query_tasks_invoked_and_metric_incremented(self):
        """LLM emits ``mcp_query_tasks`` tool_call → coder_node invokes the
        tool, appends a ToolMessage with the result, increments the success
        metric, and returns ``Command(goto="supervisor")``.
        """
        # 1. Build a fake MCP tool that returns a deterministic JSON payload.
        mcp_tool = _mcp_tool(
            "mcp_query_tasks",
            return_value='[{"id": "t-001", "status": "completed"}]',
        )

        # 2. Build an AIMessage with one tool_call. ToolCall shape:
        #    {"name": str, "args": dict, "id": str, "type": "tool_call"}
        #    (see langchain_core.messages.tool.ToolCall TypedDict).
        ai_response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "mcp_query_tasks",
                    "args": {"status": "completed", "limit": 5},
                    "id": "call-001",
                    "type": "tool_call",
                }
            ],
        )

        ctx = _build_ctx_with_mcp_tool([mcp_tool], llm_response=ai_response)
        node = make_coder_node(ctx)

        before = _mcp_tool_calls_metric_value("mcp_query_tasks", "success")

        with _PCODER_PATCHES[0], _PCODER_PATCHES[1], _PCODER_PATCHES[2]:
            result = await node(
                _base_state(),
                config={"configurable": {"thread_id": "th-test"}},
            )

        # 3. Tool was invoked exactly once with the LLM-provided args.
        mcp_tool.ainvoke.assert_awaited_once()
        invoked_args, invoked_kwargs = mcp_tool.ainvoke.await_args
        assert invoked_args[0] == {"status": "completed", "limit": 5}

        # 4. Metric incremented by exactly 1.
        after = _mcp_tool_calls_metric_value("mcp_query_tasks", "success")
        assert after - before == 1.0, f"expected +1, got delta={after - before}"

        # 5. Command shape correct.
        assert isinstance(result, Command)
        assert result.goto == "supervisor"

        # 6. ToolMessage content contains the MCP return value.
        tool_msgs = [m for m in result.update["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        assert "t-001" in tool_msgs[0].content
        assert tool_msgs[0].tool_call_id == "call-001"

        # 7. Failure metric NOT incremented for this tool.
        failed_after = _mcp_tool_calls_metric_value("mcp_query_tasks", "failed")
        assert failed_after == 0.0, "failed metric should remain 0 for successful call"


# ---------------------------------------------------------------------------
# Fan-out: multiple MCP tool_calls in a single AIMessage
# ---------------------------------------------------------------------------


class TestCoderNodeMcpTrajectoryFanOut:
    """Verify multiple MCP tool invocations in one LLM response."""

    async def test_multiple_mcp_tools_in_single_response(self):
        """LLM emits 2 ``mcp_*`` tool_calls in one AIMessage → both invoked,
        both success metrics incremented, 2 ToolMessages in update, tool_call_ids
        not mixed up.
        """
        mcp_tool_a = _mcp_tool("mcp_git_log", return_value="abc123 HEAD")
        mcp_tool_b = _mcp_tool("mcp_query_tasks", return_value='[{"id": "t-002"}]')

        ai_response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "mcp_git_log",
                    "args": {"max_count": 3},
                    "id": "c1",
                    "type": "tool_call",
                },
                {
                    "name": "mcp_query_tasks",
                    "args": {"limit": 10},
                    "id": "c2",
                    "type": "tool_call",
                },
            ],
        )

        ctx = _build_ctx_with_mcp_tool([mcp_tool_a, mcp_tool_b], llm_response=ai_response)
        node = make_coder_node(ctx)

        before_log = _mcp_tool_calls_metric_value("mcp_git_log", "success")
        before_q = _mcp_tool_calls_metric_value("mcp_query_tasks", "success")

        with _PCODER_PATCHES[0], _PCODER_PATCHES[1], _PCODER_PATCHES[2]:
            result = await node(
                _base_state(),
                config={"configurable": {"thread_id": "th-test"}},
            )

        # Both tools invoked.
        mcp_tool_a.ainvoke.assert_awaited_once()
        mcp_tool_b.ainvoke.assert_awaited_once()

        # Both success metrics incremented by 1.
        assert _mcp_tool_calls_metric_value("mcp_git_log", "success") - before_log == 1.0
        assert _mcp_tool_calls_metric_value("mcp_query_tasks", "success") - before_q == 1.0

        # 2 ToolMessages, tool_call_ids not mixed.
        tool_msgs = [m for m in result.update["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 2
        assert {m.tool_call_id for m in tool_msgs} == {"c1", "c2"}

        # Content mapping is correct (git_log → abc123, query_tasks → t-002).
        content_by_id = {m.tool_call_id: m.content for m in tool_msgs}
        assert "abc123" in content_by_id["c1"]
        assert "t-002" in content_by_id["c2"]


# ---------------------------------------------------------------------------
# Failure path: MCP tool raises
# ---------------------------------------------------------------------------


class TestCoderNodeMcpTrajectoryFailure:
    """Verify MCP tool failure is properly recorded and surfaced."""

    async def test_mcp_tool_exception_increments_failed_metric(self):
        """When the MCP tool raises, coder_node must:

        - NOT re-raise (swallow the exception, write error ToolMessage)
        - increment ``mcp_tool_calls_total{tool, status="failed"}``
        - leave the success metric at 0
        - return ``Command(goto="supervisor")`` with the error in ToolMessage
        """
        mcp_tool = _mcp_tool(
            "mcp_git_log",
            exc=RuntimeError("MCP server unreachable: ConnectionRefusedError"),
        )

        ai_response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "mcp_git_log",
                    "args": {"max_count": 5},
                    "id": "c-fail",
                    "type": "tool_call",
                }
            ],
        )

        ctx = _build_ctx_with_mcp_tool([mcp_tool], llm_response=ai_response)
        node = make_coder_node(ctx)

        before_failed = _mcp_tool_calls_metric_value("mcp_git_log", "failed")
        before_success = _mcp_tool_calls_metric_value("mcp_git_log", "success")

        with _PCODER_PATCHES[0], _PCODER_PATCHES[1], _PCODER_PATCHES[2]:
            result = await node(
                _base_state(),
                config={"configurable": {"thread_id": "th-test"}},
            )

        # Failed metric incremented by 1.
        after_failed = _mcp_tool_calls_metric_value("mcp_git_log", "failed")
        assert after_failed - before_failed == 1.0, f"expected +1 failed, got delta={after_failed - before_failed}"

        # Success metric NOT incremented.
        after_success = _mcp_tool_calls_metric_value("mcp_git_log", "success")
        assert after_success - before_success == 0.0, "success metric should not change for failed call"

        # ToolMessage contains the error text.
        tool_msgs = [m for m in result.update["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        assert "MCP server unreachable" in tool_msgs[0].content
        assert tool_msgs[0].tool_call_id == "c-fail"

        # Still routes to supervisor (graceful degradation, no crash).
        assert isinstance(result, Command)
        assert result.goto == "supervisor"
