"""Unit tests for the supervisor node — routing & completion decisions.

Covers:
- Auto-retry: validation failed + retries remaining → Command(goto="coder")
- Auto-retry exhausted: passes through to LLM routing
- LLM returns is_complete=True → Command(goto=END)
- LLM returns next_agent="coder" → Command(goto="coder")
- LLM returns next_agent="END" → Command(goto=END)
- LLM failure → error Command(goto="supervisor")
- Status update defaults to current state when None
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langgraph.graph import END

from app.core.config import settings
from app.core.langgraph.agents.context import AgentContext
from app.core.langgraph.agents.supervisor_node import make_supervisor_node
from app.schemas.sdlc import SupervisorDecision


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


def _base_state(**overrides: Any) -> dict[str, Any]:
    """Minimal SDLCState for supervisor input."""
    state: dict[str, Any] = {
        "messages": [{"role": "user", "content": "build it"}],
        "status": "coding",
        "requirements": "build it",
        "plan": [],
        "code_changes": [],
        "test_results": [],
        "feedback": [],
        "errors": [],
        "artifacts": [],
        "coder_retry_count": 0,
        "validation_results": [],
    }
    state.update(overrides)
    return state


def _config(thread_id: str = "test-thread-001") -> dict[str, Any]:
    """LangGraph RunnableConfig with thread_id."""
    return {"configurable": {"thread_id": thread_id}}


class TestSupervisorAutoRetry:
    """Tests for the validation-failed auto-retry path."""

    async def test_validation_failed_routes_to_coder(self):
        """Validation failed + retries remaining → Command(goto='coder')."""
        ctx = _build_ctx()  # LLM not called on this path
        node = make_supervisor_node(ctx)

        state = _base_state(
            validation_results=[{"passed": False, "message": "tests failed"}],
            coder_retry_count=0,
        )

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(state, _config())

        assert result.goto == "coder"
        assert result.update["status"] == "coding"
        # Feedback describes the validation failure
        feedback = result.update["feedback"]
        assert len(feedback) == 1
        assert "Validation failed" in feedback[0]["content"]

    async def test_validation_failed_max_retries_passes_to_llm(self):
        """When retries are exhausted, the LLM is consulted (no auto-retry)."""
        decision = SupervisorDecision(next_agent="reviewer", reasoning="retries exhausted", is_complete=False)
        ctx = _build_ctx(llm_response=decision)
        node = make_supervisor_node(ctx)

        state = _base_state(
            validation_results=[{"passed": False, "message": "still failing"}],
            coder_retry_count=settings.MAX_CODER_RETRIES,
        )

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(state, _config())

        # LLM was called and returned reviewer routing
        assert result.goto == "reviewer"
        ctx.llm_service.call.assert_awaited_once()

    async def test_validation_passed_does_not_auto_retry(self):
        """When validation passed, the LLM is consulted normally."""
        decision = SupervisorDecision(next_agent="reviewer", reasoning="done", is_complete=False)
        ctx = _build_ctx(llm_response=decision)
        node = make_supervisor_node(ctx)

        state = _base_state(validation_results=[{"passed": True, "message": "ok"}])

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(state, _config())

        assert result.goto == "reviewer"
        ctx.llm_service.call.assert_awaited_once()


class TestSupervisorRouting:
    """Tests for LLM-based routing decisions."""

    async def test_routes_to_coder(self):
        """next_agent='coder' → Command(goto='coder')."""
        decision = SupervisorDecision(next_agent="coder", reasoning="needs code", status_update="coding")
        ctx = _build_ctx(llm_response=decision)
        node = make_supervisor_node(ctx)

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state(), _config())

        assert result.goto == "coder"
        assert result.update["status"] == "coding"

    async def test_routes_to_tester(self):
        """next_agent='tester' → Command(goto='tester')."""
        decision = SupervisorDecision(next_agent="tester", reasoning="needs tests", status_update="testing")
        ctx = _build_ctx(llm_response=decision)
        node = make_supervisor_node(ctx)

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state(), _config())

        assert result.goto == "tester"
        assert result.update["status"] == "testing"

    async def test_is_complete_routes_to_end(self):
        """is_complete=True → Command(goto=END)."""
        decision = SupervisorDecision(next_agent="END", reasoning="all done", is_complete=True)
        ctx = _build_ctx(llm_response=decision)
        node = make_supervisor_node(ctx)

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(
                _base_state(
                    plan=[{"step": "done"}],
                    code_changes=[
                        {"file_path": "done.py", "change_type": "create", "content": "", "commit_message": ""}
                    ],
                    test_results=[{"passed": True, "message": "ok"}],
                    feedback=[{"agent_name": "reviewer", "content": "ok", "severity": "low"}],
                ),
                _config(),
            )

        assert result.goto == END
        assert result.update["is_complete"] is True
        assert result.update["status"] == "completed"

    async def test_next_agent_end_routes_to_end_even_if_not_complete(self):
        """next_agent='END' alone (without is_complete) routes to END."""
        decision = SupervisorDecision(next_agent="END", reasoning="stopping", is_complete=False)
        ctx = _build_ctx(llm_response=decision)
        node = make_supervisor_node(ctx)

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(
                _base_state(
                    plan=[{"step": "done"}],
                    code_changes=[
                        {"file_path": "done.py", "change_type": "create", "content": "", "commit_message": ""}
                    ],
                    test_results=[{"passed": True, "message": "ok"}],
                    feedback=[{"agent_name": "reviewer", "content": "ok", "severity": "low"}],
                ),
                _config(),
            )

        assert result.goto == END

    async def test_status_update_none_preserves_current_status(self):
        """When status_update is None, the current state status is preserved."""
        decision = SupervisorDecision(next_agent="coder", reasoning="continue", status_update=None)
        ctx = _build_ctx(llm_response=decision)
        node = make_supervisor_node(ctx)

        state = _base_state(status="custom_status")

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(state, _config())

        assert result.update["status"] == "custom_status"


class TestSupervisorFailure:
    """Tests for the supervisor's error handling."""

    async def test_llm_exception_returns_error_command(self):
        """When LLM raises, an error Command is returned (not re-raised)."""
        ctx = _build_ctx(llm_exc=RuntimeError("LLM down"))
        node = make_supervisor_node(ctx)

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state(), _config())

        assert result.goto == "supervisor"
        errors = result.update["errors"]
        assert len(errors) == 1
        assert errors[0]["agent_name"] == "supervisor"
        assert "LLM down" in errors[0]["content"]


class TestSupervisorModelRouter:
    """Tests verifying model_router is consulted."""

    async def test_model_router_consulted_for_supervisor(self):
        """The model router is queried with role='supervisor'."""
        decision = SupervisorDecision(next_agent="coder", reasoning="go", is_complete=False)
        ctx = _build_ctx(llm_response=decision)
        node = make_supervisor_node(ctx)

        with (
            patch("app.core.langgraph.agents.supervisor_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.supervisor_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            await node(_base_state(), _config())

        ctx.model_router.get_model_for_call.assert_called_once_with("supervisor")
