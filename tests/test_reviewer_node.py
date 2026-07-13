"""Unit tests for the reviewer node — quality review & human-in-the-loop.

Covers:
- Happy path: low/medium feedback → Command(goto="supervisor"), no interrupt
- High severity feedback → langgraph_interrupt() is triggered
- Critical severity feedback → langgraph_interrupt() is triggered
- Empty feedback → succeeds, human_approval_needed=False
- LLM failure → error Command(goto="supervisor")
- Verify set_llm_context wraps the LLM call (agent_name="reviewer")
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.graph.state import Command

from app.core.langgraph.agents.context import AgentContext
from app.core.langgraph.agents.reviewer_node import make_reviewer_node
from app.schemas.sdlc import FeedbackItem, ReviewerOutput


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
    """Minimal SDLCState for reviewer input."""
    return {
        "messages": [{"role": "user", "content": "review this code"}],
        "requirements": "build it",
        "plan": [],
        "code_changes": [],
        "test_results": [],
    }


class TestReviewerNodeHappyPath:
    """Tests for the reviewer's success path with non-blocking feedback."""

    async def test_low_severity_returns_command_goto_supervisor(self):
        """Low-severity feedback does NOT trigger interrupt."""
        review = ReviewerOutput(
            feedback=[FeedbackItem(agent_name="reviewer", content="minor nit", severity="low")],
            summary="looks ok",
        )
        ctx = _build_ctx(llm_response=review)
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert isinstance(result, Command)
        assert result.goto == "supervisor"
        assert result.update["human_approval_needed"] is False

    async def test_medium_severity_no_interrupt(self):
        """Medium-severity feedback does NOT trigger interrupt."""
        review = ReviewerOutput(
            feedback=[FeedbackItem(agent_name="reviewer", content="could improve", severity="medium")],
            summary="acceptable",
        )
        ctx = _build_ctx(llm_response=review)
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert result.goto == "supervisor"
        assert result.update["human_approval_needed"] is False

    async def test_update_contains_feedback_and_summary(self):
        """Command.update has the feedback list and summary message."""
        review = ReviewerOutput(
            feedback=[
                FeedbackItem(agent_name="reviewer", content="issue 1", severity="low"),
                FeedbackItem(agent_name="reviewer", content="issue 2", severity="medium"),
            ],
            summary="2 issues found",
        )
        ctx = _build_ctx(llm_response=review)
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        update = result.update
        assert len(update["feedback"]) == 2
        assert update["feedback"][0]["content"] == "issue 1"
        assert update["messages"][0]["content"] == "2 issues found"
        assert update["status"] == "reviewing"

    async def test_empty_feedback_succeeds(self):
        """No feedback items → succeeds, no approval needed."""
        review = ReviewerOutput(feedback=[], summary="all good")
        ctx = _build_ctx(llm_response=review)
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert result.goto == "supervisor"
        assert result.update["feedback"] == []
        assert result.update["human_approval_needed"] is False


class TestReviewerNodeHumanInterruption:
    """Tests for the high/critical severity interrupt path."""

    async def test_high_severity_triggers_interrupt(self):
        """High-severity feedback triggers langgraph_interrupt()."""
        review = ReviewerOutput(
            feedback=[FeedbackItem(agent_name="reviewer", content="security hole", severity="high")],
            summary="needs approval",
        )
        ctx = _build_ctx(llm_response=review)
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
            patch(
                "app.core.langgraph.agents.reviewer_node.langgraph_interrupt", side_effect=GraphInterrupt("approval")
            ),
        ):
            with pytest.raises(GraphInterrupt):
                await node(_base_state())

    async def test_critical_severity_triggers_interrupt(self):
        """Critical-severity feedback triggers langgraph_interrupt()."""
        review = ReviewerOutput(
            feedback=[FeedbackItem(agent_name="reviewer", content="data leak", severity="critical")],
            summary="critical issue",
        )
        ctx = _build_ctx(llm_response=review)
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
            patch(
                "app.core.langgraph.agents.reviewer_node.langgraph_interrupt", side_effect=GraphInterrupt("approval")
            ),
        ):
            with pytest.raises(GraphInterrupt):
                await node(_base_state())

    async def test_mixed_severity_with_one_high_triggers_interrupt(self):
        """One high-severity item in a mixed list triggers interrupt."""
        review = ReviewerOutput(
            feedback=[
                FeedbackItem(agent_name="reviewer", content="low issue", severity="low"),
                FeedbackItem(agent_name="reviewer", content="high issue", severity="high"),
            ],
            summary="mixed",
        )
        ctx = _build_ctx(llm_response=review)
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
            patch("app.core.langgraph.agents.reviewer_node.langgraph_interrupt", side_effect=GraphInterrupt("stop")),
        ):
            with pytest.raises(GraphInterrupt):
                await node(_base_state())


class TestReviewerNodeFailure:
    """Tests for the reviewer's error handling."""

    async def test_llm_exception_returns_error_command(self):
        """When LLM raises, an error Command is returned (not re-raised)."""
        ctx = _build_ctx(llm_exc=RuntimeError("LLM unavailable"))
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert result.goto == "supervisor"
        errors = result.update["errors"]
        assert len(errors) == 1
        assert errors[0]["agent_name"] == "reviewer"
        assert "LLM unavailable" in errors[0]["content"]

    async def test_graph_interrupt_is_re_raised(self):
        """GraphInterrupt is re-raised, not swallowed by the except clause."""
        ctx = _build_ctx(llm_exc=GraphInterrupt("approval"))
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            with pytest.raises(GraphInterrupt):
                await node(_base_state())


class TestReviewerNodeContextPropagation:
    """Tests verifying set_llm_context wraps the LLM call."""

    async def test_set_llm_context_entered_for_reviewer(self):
        """The LLM call is wrapped in set_llm_context('reviewer', 'reviewing')."""
        review = ReviewerOutput(feedback=[], summary="ok")
        ctx = _build_ctx(llm_response=review)
        node = make_reviewer_node(ctx)

        from contextlib import contextmanager

        from app.services.llm.usage_tracker import set_llm_context as original_set

        captured: list[tuple[str, str]] = []

        @contextmanager
        def spy_context(agent_name: str, task_type: str = "general"):
            captured.append((agent_name, task_type))
            with original_set(agent_name, task_type):
                yield

        with (
            patch("app.core.langgraph.agents.context.set_llm_context", spy_context),
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            await node(_base_state())

        assert captured == [("reviewer", "reviewing")]

    async def test_model_router_consulted_for_reviewer(self):
        """The model router is queried with role='reviewer'."""
        review = ReviewerOutput(feedback=[], summary="ok")
        ctx = _build_ctx(llm_response=review)
        node = make_reviewer_node(ctx)

        with (
            patch("app.core.langgraph.agents.reviewer_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.reviewer_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            await node(_base_state())

        ctx.model_router.get_model_for_call.assert_called_once_with("reviewer")
