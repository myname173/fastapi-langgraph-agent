"""Unit tests for the planner node — structured plan generation.

Covers:
- Happy path: LLM returns a valid PlannerOutput → Command(goto="supervisor")
- Empty plan: LLM returns no tasks → plan=[] but still succeeds
- LLM failure: exception → error Command with errors list
- Verify set_llm_context is entered (agent_name="planner")
- Verify model_router.get_model_for_call("planner") is consulted
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langgraph.graph.state import Command

from app.core.langgraph.agents.context import AgentContext
from app.core.langgraph.agents.planner_node import make_planner_node
from app.schemas.sdlc import PlannerOutput, Task


def _build_ctx(llm_response: Any = None, llm_exc: Exception | None = None) -> AgentContext:
    """Build an AgentContext with a mocked llm_service and model_router."""
    mock_llm = MagicMock()
    mock_llm.call = AsyncMock(return_value=llm_response, side_effect=llm_exc)
    mock_router = MagicMock()
    mock_router.get_model_for_call = MagicMock(return_value=None)
    return AgentContext(
        llm_service=mock_llm,
        registry=MagicMock(),
        code_index_service=MagicMock(),
        smart_search_service=MagicMock(),
        knowledge_base_service=MagicMock(),
        model_router=mock_router,
    )


def _base_state() -> dict[str, Any]:
    """Minimal SDLCState for planner input."""
    return {
        "messages": [{"role": "user", "content": "build a hello world API"}],
        "requirements": "build a hello world API",
    }


class TestPlannerNodeHappyPath:
    """Tests for the planner's success path."""

    async def test_returns_command_goto_supervisor(self):
        """Successful planning returns Command(goto='supervisor')."""
        plan = PlannerOutput(
            plan=[Task(id="t1", description="step 1"), Task(id="t2", description="step 2")],
            summary="2-step plan",
        )
        ctx = _build_ctx(llm_response=plan)
        node = make_planner_node(ctx)

        with (
            patch("app.core.langgraph.agents.planner_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.planner_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert isinstance(result, Command)
        assert result.goto == "supervisor"

    async def test_update_contains_plan_and_summary(self):
        """Command.update has the plan list, summary message, and status."""
        plan = PlannerOutput(
            plan=[Task(id="t1", description="step 1", status="pending")],
            summary="plan summary text",
        )
        ctx = _build_ctx(llm_response=plan)
        node = make_planner_node(ctx)

        with (
            patch("app.core.langgraph.agents.planner_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.planner_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        update = result.update
        assert update["status"] == "planning"
        assert len(update["plan"]) == 1
        assert update["plan"][0]["id"] == "t1"
        # messages is a list of dicts with role/content
        assert update["messages"][0]["role"] == "assistant"
        assert update["messages"][0]["content"] == "plan summary text"

    async def test_empty_plan_still_succeeds(self):
        """When LLM returns no tasks, plan=[] but the node still succeeds."""
        plan = PlannerOutput(plan=[], summary="no tasks needed")
        ctx = _build_ctx(llm_response=plan)
        node = make_planner_node(ctx)

        with (
            patch("app.core.langgraph.agents.planner_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.planner_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert result.goto == "supervisor"
        assert result.update["plan"] == []

    async def test_model_router_consulted_for_planner(self):
        """The model router is queried with role='planner'."""
        plan = PlannerOutput(plan=[], summary="ok")
        ctx = _build_ctx(llm_response=plan)
        node = make_planner_node(ctx)

        with (
            patch("app.core.langgraph.agents.planner_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.planner_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            await node(_base_state())

        ctx.model_router.get_model_for_call.assert_called_once_with("planner")


class TestPlannerNodeFailure:
    """Tests for the planner's error handling."""

    async def test_llm_exception_returns_error_command(self):
        """When LLM raises, an error Command is returned (not re-raised)."""
        ctx = _build_ctx(llm_exc=RuntimeError("LLM is down"))
        node = make_planner_node(ctx)

        with (
            patch("app.core.langgraph.agents.planner_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.planner_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert result.goto == "supervisor"
        assert result.update["status"] == "planning"
        # errors list contains the exception message
        errors = result.update["errors"]
        assert len(errors) == 1
        assert errors[0]["agent_name"] == "planner"
        assert "LLM is down" in errors[0]["content"]
        assert errors[0]["severity"] == "high"

    async def test_llm_exception_message_in_assistant_content(self):
        """The error message is also surfaced in the assistant message."""
        ctx = _build_ctx(llm_exc=ValueError("bad response"))
        node = make_planner_node(ctx)

        with (
            patch("app.core.langgraph.agents.planner_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.planner_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assistant_msg = result.update["messages"][0]
        assert "Planner error" in assistant_msg["content"]
        assert "bad response" in assistant_msg["content"]


class TestPlannerFeasibilityGate:
    """Tests for the technical feasibility gate (feasibility field)."""

    async def test_blocked_sets_human_approval_needed(self):
        """When feasibility=='blocked', human_approval_needed is set to True."""
        plan = PlannerOutput(
            plan=[],
            summary="blocked",
            feasibility="blocked",
            feasibility_rationale="External API unavailable; no fallback path.",
        )
        ctx = _build_ctx(llm_response=plan)
        node = make_planner_node(ctx)

        with (
            patch("app.core.langgraph.agents.planner_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.planner_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert result.goto == "supervisor"
        assert result.update["human_approval_needed"] is True
        assert result.update["plan"] == []

    async def test_feasible_does_not_set_approval(self):
        """When feasibility=='feasible', human_approval_needed is not set."""
        plan = PlannerOutput(
            plan=[Task(id="t1", description="step 1")],
            summary="ok",
            feasibility="feasible",
            feasibility_rationale="Standard FastAPI endpoint addition.",
        )
        ctx = _build_ctx(llm_response=plan)
        node = make_planner_node(ctx)

        with (
            patch("app.core.langgraph.agents.planner_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.planner_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert result.update.get("human_approval_needed", False) is False
        assert len(result.update["plan"]) == 1


class TestPlannerNodeContextPropagation:
    """Tests verifying set_llm_context wraps the LLM call."""

    async def test_set_llm_context_entered_for_planner(self):
        """The LLM call is wrapped in set_llm_context('planner', 'planning')."""
        plan = PlannerOutput(plan=[], summary="ok")
        ctx = _build_ctx(llm_response=plan)
        node = make_planner_node(ctx)

        # Patch set_llm_context to spy on the call — use a real context manager
        # so the code path is unchanged.
        from app.services.llm.usage_tracker import set_llm_context

        captured: list[tuple[str, str]] = []
        original_set = set_llm_context

        from contextlib import contextmanager

        @contextmanager
        def spy_context(agent_name: str, task_type: str = "general"):
            captured.append((agent_name, task_type))
            with original_set(agent_name, task_type):
                yield

        with (
            patch("app.core.langgraph.agents.context.set_llm_context", spy_context),
            patch("app.core.langgraph.agents.planner_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.planner_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            await node(_base_state())

        assert captured == [("planner", "planning")]
