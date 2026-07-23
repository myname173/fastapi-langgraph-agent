"""Trajectory-level regression gate for the eval framework.

This is the **deterministic** half of the trajectory eval upgrade (path C
hybrid). It runs entirely on mock LLM/tool infrastructure — zero token cost,
zero network, CI-safe — and locks in the expected agent behaviour for the
golden dataset. If a ``coder_node`` refactor changes which tools get
dispatched or in what order, these tests catch it before merge.

Two test groups:

1. **Golden dataset integrity** — the 8 golden tasks in
   ``evals/golden/tasks.py`` are self-consistent: required fields present,
   step count within budget, no redundant loops, token estimate within
   budget, all tool names registered.
2. **Coder trajectory replay** — for each golden task, a mock LLM emits the
   expected tool-calls and ``coder_node`` is asserted to dispatch them in
   the expected order with the expected args.

The mock pattern mirrors ``tests/test_coder_node_mcp_trajectory.py`` but is
self-contained (decision D) because golden tasks mix file tools
(``write_file``/``edit_file``/``read_file``/``run_tests``) with MCP tools
(``mcp_query_tasks``), unlike the MCP-only existing test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph.state import Command

from app.core.langgraph.agents.context import AgentContext
from app.core.langgraph.agents.coder_node import make_coder_node
from evals.golden.tasks import GOLDEN_TASKS, GoldenTask
from evals.trajectory_helpers import (
    detect_redundant_loops,
    estimate_tokens,
)


# ---------------------------------------------------------------------------
# Known tool names — the set registered in app/core/langgraph/tools/sdlc_tools.py
# plus any dynamically-registered ``mcp_*`` tool. Used by the tool-name
# sanity check so a typo'd tool name in a golden task fails CI.
# ---------------------------------------------------------------------------

_KNOWN_FILE_TOOLS: set[str] = {
    "read_file",
    "write_file",
    "edit_file",
    "list_directory",
    "search_code",
    "security_scan",
    "run_tests",
    "git_status",
    "git_diff",
    "apply_multi_file_edits",
    "validate_changes",
    "generate_and_run_script",
    "save_output_file",
    "analyze_image",
}


def _is_known_tool(name: str) -> bool:
    """True if ``name`` is a registered file tool or an ``mcp_*`` tool."""
    return name in _KNOWN_FILE_TOOLS or name.startswith("mcp_")


# ---------------------------------------------------------------------------
# Mock infrastructure (self-contained — decision D)
# ---------------------------------------------------------------------------


def _mock_tool(name: str) -> MagicMock:
    """Build a fake LangChain tool with the given name.

    The tool's ``ainvoke`` returns a deterministic string so the resulting
    ToolMessage content is predictable.

    Args:
        name: Tool name (must be a known file tool or ``mcp_*``).

    Returns:
        A MagicMock shaped like a LangChain BaseTool (``.name`` + awaitable
        ``.ainvoke``).
    """
    tool = MagicMock()
    tool.name = name
    tool.ainvoke = AsyncMock(return_value=f"ok:{name}")
    return tool


def _golden_tool_calls(task: GoldenTask) -> list[dict]:
    """Extract the assistant tool-call steps from a golden trajectory.

    Args:
        task: The golden task whose trajectory to read.

    Returns:
        A list of langchain ToolCall dicts (``{name, args, id, type}``),
        one per assistant tool-call step, preserving order.
    """
    calls: list[dict] = []
    for idx, step in enumerate(task.expected_trajectory, start=1):
        if step.get("type") == "assistant" and "tool" in step:
            calls.append(
                {
                    "name": step["tool"],
                    "args": step.get("args", {}),
                    "id": f"call-{task.task_type}-{idx}",
                    "type": "tool_call",
                }
            )
    return calls


def _build_coder_ctx(task: GoldenTask) -> tuple[AgentContext, list[MagicMock]]:
    """Build an AgentContext whose registry serves all of ``task.expected_tools``.

    The mock LLM is wired to return an AIMessage carrying the golden
    tool-calls when ``ainvoke`` is called.

    Args:
        task: The golden task to replay.

    Returns:
        Tuple of (AgentContext, mock_tools). ``mock_tools`` is the list of
        fake tools registered for the coder, so the test can assert
        ``ainvoke`` call args afterwards.
    """
    mock_tools = [_mock_tool(name) for name in task.expected_tools]

    ai_response = AIMessage(content="", tool_calls=_golden_tool_calls(task))

    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=ai_response)
    mock_llm = MagicMock()
    mock_llm.bind_tools = MagicMock(return_value=bound)

    mock_llm_service = MagicMock()
    mock_llm_service.get_llm_for = MagicMock(return_value=mock_llm)

    mock_registry = MagicMock()
    mock_registry.get_for_agent = MagicMock(return_value=mock_tools)
    # Defensive: _apply_smart_edits_and_validate may call get_by_name, but
    # the tool_calls branch returns before reaching it — still mock to be
    # safe against future refactors.
    mock_registry.get_by_name = MagicMock(return_value=MagicMock(ainvoke=AsyncMock(return_value={})))

    mock_router = MagicMock()
    mock_router.get_model_for = MagicMock(return_value="test-model")
    mock_router.get_model_for_call = MagicMock(return_value=None)

    ctx = AgentContext(
        llm_service=mock_llm_service,
        registry=mock_registry,
        code_index_service=MagicMock(),
        smart_search_service=MagicMock(),
        knowledge_base_service=MagicMock(),
        model_router=mock_router,
    )
    return ctx, mock_tools


def _base_state(task: GoldenTask) -> dict[str, Any]:
    """Minimal SDLCState for coder input, seeded from the golden task."""
    return {
        "messages": [{"role": "user", "content": task.requirement}],
        "requirements": task.requirement,
        "plan": [],
        "code_changes": [],
        "coder_retry_count": 0,
    }


# Patches for coder_node internals that touch real services. Same set as
# test_coder_node_mcp_trajectory.py — load_agent_prompt / prepare_sdlc_messages
# / _build_code_index_context must be stubbed or the MagicMock services would
# cascade into unexpected branches.
_CODER_PATCHES = (
    patch("app.core.langgraph.agents.coder_node.load_agent_prompt", return_value="sys"),
    patch("app.core.langgraph.agents.coder_node.prepare_sdlc_messages", return_value=[]),
    patch(
        "app.core.langgraph.agents.coder_node._build_code_index_context",
        AsyncMock(return_value=""),
    ),
)


# ---------------------------------------------------------------------------
# Group 1: Golden dataset integrity
# ---------------------------------------------------------------------------


@pytest.mark.regression
class TestGoldenDatasetIntegrity:
    """The golden dataset itself is well-formed."""

    def test_golden_tasks_complete(self):
        """All golden tasks have required fields with sane values."""
        assert len(GOLDEN_TASKS) >= 8, f"expected >=8 golden tasks, got {len(GOLDEN_TASKS)}"
        for task in GOLDEN_TASKS:
            assert task.task_type, f"task missing task_type: {task}"
            assert task.requirement, f"{task.task_type}: missing requirement"
            assert task.expected_files, f"{task.task_type}: missing expected_files"
            assert task.expected_tools, f"{task.task_type}: missing expected_tools"
            assert task.expected_trajectory, f"{task.task_type}: missing trajectory"
            assert task.max_steps > 0, f"{task.task_type}: max_steps must be > 0"
            assert task.max_tokens > 0, f"{task.task_type}: max_tokens must be > 0"

    @pytest.mark.parametrize("task", GOLDEN_TASKS, ids=lambda t: t.task_type)
    def test_max_steps_respected(self, task: GoldenTask):
        """Trajectory length must not exceed the declared step budget."""
        assert len(task.expected_trajectory) <= task.max_steps, (
            f"{task.task_type}: trajectory has {len(task.expected_trajectory)} steps but max_steps={task.max_steps}"
        )

    @pytest.mark.parametrize("task", GOLDEN_TASKS, ids=lambda t: t.task_type)
    def test_no_redundant_loops(self, task: GoldenTask):
        """No consecutive identical tool-calls in the expected trajectory."""
        loops = detect_redundant_loops(task.expected_trajectory)
        assert loops == [], f"{task.task_type}: redundant loops detected at steps {loops}"

    @pytest.mark.parametrize("task", GOLDEN_TASKS, ids=lambda t: t.task_type)
    def test_max_tokens_budget(self, task: GoldenTask):
        """Estimated token usage of requirement + trajectory within budget."""
        est = estimate_tokens(task.requirement)
        est += sum(estimate_tokens(str(step)) for step in task.expected_trajectory)
        assert est <= task.max_tokens, f"{task.task_type}: estimated {est} tokens exceeds max_tokens={task.max_tokens}"

    def test_expected_tools_are_known(self):
        """Every tool in every golden task is a registered tool name."""
        unknown: list[str] = []
        for task in GOLDEN_TASKS:
            for tool in task.expected_tools:
                if not _is_known_tool(tool):
                    unknown.append(f"{task.task_type}:{tool}")
        assert unknown == [], f"unknown tool names in golden tasks: {unknown}"


# ---------------------------------------------------------------------------
# Group 2: Coder trajectory replay
# ---------------------------------------------------------------------------


@pytest.mark.regression
class TestCoderTrajectoryReplay:
    """coder_node dispatches the expected tool-calls for each golden task."""

    @pytest.mark.parametrize("task", GOLDEN_TASKS, ids=lambda t: t.task_type)
    async def test_coder_trajectory_matches_expected(self, task: GoldenTask):
        """Mock LLM emits the golden tool-calls, coder_node produces matching ToolMessages.

        Asserts tool call count, tool names, invocation args, and ToolMessage content.
        """
        ctx, mock_tools = _build_coder_ctx(task)
        node = make_coder_node(ctx)

        expected_calls = _golden_tool_calls(task)

        with _CODER_PATCHES[0], _CODER_PATCHES[1], _CODER_PATCHES[2]:
            result = await node(
                _base_state(task),
                config={"configurable": {"thread_id": "th-test"}},
            )

        # 1. Command shape correct.
        assert isinstance(result, Command)
        assert result.goto == "supervisor"

        # 2. ToolMessages match the expected tool sequence (order + count).
        tool_msgs = [m for m in result.update["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == len(expected_calls), (
            f"{task.task_type}: expected {len(expected_calls)} ToolMessages, got {len(tool_msgs)}"
        )
        # 3. Each expected tool was invoked exactly once with the expected args.
        tools_by_name = {t.name: t for t in mock_tools}
        for idx, call in enumerate(expected_calls):
            tool = tools_by_name[call["name"]]
            assert tool.ainvoke.await_count >= 1, f"{task.task_type}: tool {call['name']} was never invoked"
            invoked_args, _ = tool.ainvoke.await_args
            assert invoked_args[0] == call["args"], (
                f"{task.task_type} step {idx}: tool {call['name']} invoked with "
                f"{invoked_args[0]}, expected {call['args']}"
            )

        # 4. Each ToolMessage content reflects its tool's return value.
        for idx, msg in enumerate(tool_msgs):
            expected_name = expected_calls[idx]["name"]
            assert f"ok:{expected_name}" in msg.content, (
                f"{task.task_type}: ToolMessage {idx} content {msg.content!r} "
                f"missing expected return for {expected_name}"
            )
