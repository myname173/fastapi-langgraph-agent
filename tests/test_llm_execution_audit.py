"""Regression tests for SDLC model-execution auditing.

Three defects conspired to make the 「模型调用审计」 panel, the task list's
「模型」 column, and the task-detail audit section permanently empty, even
though 13 SDLC tasks had completed successfully:

1. ``wrap_node_with_handoff`` annotated its ``config`` parameter as ``Any``.
   LangGraph 1.x only injects ``config`` when that parameter is annotated
   ``RunnableConfig`` (it emits a ``UserWarning`` otherwise). So
   ``execution_id`` was always ``None`` and
   ``llm_execution_service.record()`` returned early for *every* node —
   planner, supervisor, reviewer and po included.

2. ``coder_node`` / ``tester_node`` obtained the model through
   ``get_llm_for(...).bind_tools(...).ainvoke(...)``, bypassing
   ``LLMService._fallback_loop`` — the only writer of ``llm_execution`` rows.

3. ``record()`` skipped silently, so the sole symptom was an empty UI.

These tests pin all three so the audit cannot silently regress again.
"""

from typing import Any, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from app.services.llm.registry import LLMRegistry
from app.services.llm.service import llm_service


class _GraphState(TypedDict):
    x: int


# ---------------------------------------------------------------------------
# 1. The wrapper must actually receive `config` from LangGraph.
# ---------------------------------------------------------------------------


class TestNodeWrapperReceivesGraphConfig:
    """``wrap_node_with_handoff`` must see ``thread_id`` from the graph config.

    The trap is purely in the type annotation: with ``config: Any`` LangGraph
    silently passes nothing; with ``config: RunnableConfig | None`` it injects
    the real config. Nothing else about the function body differs.
    """

    async def test_wrapper_sees_thread_id_and_user_id(self) -> None:
        """The node wrapper forwards thread_id and user_id into the execution context."""
        from langgraph.graph import END, START, StateGraph

        from app.core.langgraph.agents.context import wrap_node_with_handoff
        from app.services.llm.execution_context import get_llm_execution_context

        seen: dict[str, Any] = {}

        async def node(state: Any) -> None:
            seen["ctx"] = get_llm_execution_context()

        graph = StateGraph(_GraphState)
        graph.add_node("planner", wrap_node_with_handoff("planner", node))
        graph.add_edge(START, "planner")
        graph.add_edge("planner", END)

        await graph.compile().ainvoke(
            {"x": 1},
            config={"configurable": {"thread_id": "TH-1"}, "metadata": {"user_id": 42}},
        )

        ctx = seen["ctx"]
        assert ctx.execution_id == "TH-1", (
            "LangGraph did not inject config — check that the wrapper's `config` parameter is annotated RunnableConfig"
        )
        assert ctx.user_id == 42
        assert ctx.agent_name == "planner"
        assert ctx.purpose == "planner"

    async def test_wrapper_annotation_is_runnable_config(self) -> None:
        """Guard the annotation itself, so the fix cannot be 'simplified' away."""
        import inspect

        from app.core.langgraph.agents.context import wrap_node_with_handoff

        def _node(state: Any) -> None:  # pragma: no cover - never called
            return None

        wrapped = wrap_node_with_handoff("coder", _node)
        annotation = inspect.signature(wrapped).parameters["config"].annotation
        assert annotation is not inspect.Parameter.empty
        assert "RunnableConfig" in str(annotation)


# ---------------------------------------------------------------------------
# 2. Agent nodes must route through the audited `call()` entry point.
# ---------------------------------------------------------------------------


def _coder_ctx(llm_response: object, tools: list) -> Any:
    from app.core.langgraph.agents.context import AgentContext

    mock_llm_service = MagicMock()
    mock_llm_service.call = AsyncMock(return_value=llm_response)

    mock_registry = MagicMock()
    mock_registry.get_for_agent = MagicMock(return_value=tools)
    mock_registry.get_by_name = MagicMock(return_value=MagicMock(ainvoke=AsyncMock(return_value={})))

    mock_router = MagicMock()
    mock_router.get_model_for = MagicMock(return_value="test-model")

    return AgentContext(
        llm_service=mock_llm_service,
        registry=mock_registry,
        code_index_service=MagicMock(),
        smart_search_service=MagicMock(),
        knowledge_base_service=MagicMock(),
        model_router=mock_router,
    )


class TestAgentNodesUseAuditedCall:
    """coder/tester must not bypass the audit by calling ``ainvoke`` directly."""

    async def test_coder_node_passes_agent_tools_to_call(self) -> None:
        """The coder node forwards its agent-scoped tools into the LLM call."""
        from app.core.langgraph.agents.coder_node import make_coder_node

        tool = MagicMock()
        tool.name = "list_directory"
        tool.ainvoke = AsyncMock(return_value="[]")

        ai_response = AIMessage(
            content="",
            tool_calls=[{"name": "list_directory", "args": {"path": "."}, "id": "c1", "type": "tool_call"}],
        )

        ctx = _coder_ctx(ai_response, [tool])
        node = make_coder_node(ctx)

        state = {
            "messages": [{"role": "user", "content": "list files"}],
            "requirements": "list files",
            "plan": [],
            "code_changes": [],
            "coder_retry_count": 0,
        }

        with (
            patch("app.core.langgraph.agents.coder_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.coder_node.prepare_sdlc_messages", return_value=[]),
            patch(
                "app.core.langgraph.agents.coder_node._build_code_index_context",
                AsyncMock(return_value=""),
            ),
        ):
            await node(state, config={"configurable": {"thread_id": "th-1"}})

        ctx.llm_service.call.assert_awaited()
        kwargs = ctx.llm_service.call.await_args.kwargs
        assert kwargs.get("tools") == [tool], "coder must pass its agent-scoped tools to call()"
        assert kwargs.get("model_name") == "test-model"
        # The old bypass must be gone.
        ctx.llm_service.get_llm_for.assert_not_called()

    async def test_tester_node_passes_agent_tools_to_call(self) -> None:
        """The tester node forwards its agent-scoped tools into the LLM call."""
        from app.core.langgraph.agents.tester_node import make_tester_node

        run_tests = MagicMock()
        run_tests.name = "run_tests"
        run_tests.ainvoke = AsyncMock(return_value="Return code: 0")

        ai_response = AIMessage(
            content="",
            tool_calls=[{"name": "run_tests", "args": {}, "id": "t1", "type": "tool_call"}],
        )

        ctx = _coder_ctx(ai_response, [run_tests])
        node = make_tester_node(ctx)

        state = {
            "messages": [{"role": "user", "content": "test it"}],
            "requirements": "test it",
            "plan": [],
            "code_changes": [],
            "test_output": "",
        }

        with (
            patch("app.core.langgraph.agents.tester_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.tester_node.prepare_sdlc_messages", return_value=[]),
        ):
            await node(state, config={"configurable": {"thread_id": "th-2"}})

        ctx.llm_service.call.assert_awaited()
        kwargs = ctx.llm_service.call.await_args.kwargs
        assert kwargs.get("tools") == [run_tests]
        ctx.llm_service.get_llm_for.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Tool selection inside `_build_target`.
# ---------------------------------------------------------------------------


class TestBuildTargetToolSelection:
    """Tests for explicit-tool precedence in LLMService._build_target()."""

    def test_explicit_tools_override_bound_tools(self) -> None:
        """Explicit tools win over the service-wide bound tools."""
        base = MagicMock()
        with patch.object(LLMRegistry, "get", return_value=base):
            llm_service._build_target(0, None, {}, True, ["agent-scoped"])
        base.bind_tools.assert_called_once_with(["agent-scoped"])

    def test_bind_tools_false_skips_binding_even_with_explicit_tools(self) -> None:
        """bind_tools=False skips binding even when explicit tools are given."""
        base = MagicMock()
        with patch.object(LLMRegistry, "get", return_value=base):
            llm_service._build_target(0, None, {}, False, ["agent-scoped"])
        base.bind_tools.assert_not_called()

    def test_no_tools_falls_back_to_service_bound_tools(self) -> None:
        """With no explicit tools the service-wide bound tools are used."""
        base = MagicMock()
        original = llm_service._bound_tools
        llm_service._bound_tools = ["global"]
        try:
            with patch.object(LLMRegistry, "get", return_value=base):
                llm_service._build_target(0, None, {}, True, None)
        finally:
            llm_service._bound_tools = original
        base.bind_tools.assert_called_once_with(["global"])


# ---------------------------------------------------------------------------
# 4. `record()` must stay a no-op but never be silent.
# ---------------------------------------------------------------------------


class TestRecordSkipsWithoutExecutionId:
    """Tests that the audit trail degrades safely when unconfigured."""

    async def test_no_write_and_no_raise_when_execution_id_missing(self) -> None:
        """A missing execution id skips the audit write instead of raising."""
        from app.services.llm_execution_service import llm_execution_service

        with patch("app.services.llm_execution_service.run_in_thread", new=AsyncMock()) as run_thread:
            await llm_execution_service.record(
                execution_id=None,
                user_id=1,
                agent_name="coder",
                purpose="coding",
                requested_model="m",
                effective_model="m",
                actual_model="m",
                deployment="d",
                attempt=1,
                outcome="succeeded",
                duration_ms=1,
            )

        run_thread.assert_not_awaited()

    async def test_disabled_audit_flag_skips_write(self) -> None:
        """A disabled audit flag skips the audit write."""
        from app.services.llm_execution_service import llm_execution_service

        with (
            patch("app.services.llm_execution_service.settings") as mock_settings,
            patch("app.services.llm_execution_service.run_in_thread", new=AsyncMock()) as run_thread,
        ):
            mock_settings.ENABLE_LLM_EXECUTION_AUDIT = False
            await llm_execution_service.record(
                execution_id="TH-1",
                user_id=1,
                agent_name="coder",
                purpose="coding",
                requested_model="m",
                effective_model="m",
                actual_model="m",
                deployment="d",
                attempt=1,
                outcome="succeeded",
                duration_ms=1,
            )

        run_thread.assert_not_awaited()
