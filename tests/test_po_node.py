"""Unit tests for the PO (Product Owner) node — structured spec generation.

Covers:
- Happy path: LLM returns a valid ProductSpec → Command(goto="supervisor")
- Spec content: Command.update has product_spec list, status="specing"
- LLM failure: exception → fallback Command with placeholder spec (dead-loop prevention)
- Verify model_router.get_model_for_call("po") is consulted
- Verify set_llm_context is entered with agent_name="po"
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langgraph.graph.state import Command

from app.core.langgraph.agents.context import AgentContext
from app.core.langgraph.agents.po_node import make_po_node
from app.schemas.sdlc import AcceptanceCriterion, ProductSpec


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
    """Minimal SDLCState for PO input."""
    return {
        "messages": [{"role": "user", "content": "build a hello world API"}],
        "requirements": "build a hello world API",
    }


def _sample_spec() -> ProductSpec:
    """Build a valid ProductSpec for happy-path tests."""
    return ProductSpec(
        summary="Hello world API",
        problem_statement="Users need a simple API endpoint",
        target_users=["developers"],
        success_metrics=["endpoint returns 200"],
        in_scope=["GET /hello endpoint"],
        out_of_scope=["authentication"],
        assumptions=["FastAPI framework available"],
        risks=["none"],
        acceptance_criteria=[
            AcceptanceCriterion(
                scenario="GET /hello returns greeting",
                given="the API is running",
                when="a client calls GET /hello",
                then="it returns 200 with a greeting message",
            )
        ],
        mvp_recommendation="Single GET endpoint, no auth",
        open_questions=[],
        needs_clarification=False,
        low_value_rationale=None,
    )


class TestPONodeHappyPath:
    """Tests for the PO node's success path."""

    async def test_returns_command_goto_supervisor(self):
        """Successful spec generation returns Command(goto='supervisor')."""
        ctx = _build_ctx(llm_response=_sample_spec())
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert isinstance(result, Command)
        assert result.goto == "supervisor"

    async def test_update_contains_spec_and_status(self):
        """Command.update has the product_spec list and status='specing'."""
        spec = _sample_spec()
        ctx = _build_ctx(llm_response=spec)
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        update = result.update
        assert update["status"] == "specing"
        # product_spec is a list with one dict (model_dump of ProductSpec)
        assert len(update["product_spec"]) == 1
        spec_dict = update["product_spec"][0]
        assert spec_dict["summary"] == "Hello world API"
        assert len(spec_dict["acceptance_criteria"]) == 1
        assert spec_dict["acceptance_criteria"][0]["then"] == "it returns 200 with a greeting message"

    async def test_messages_contains_summary(self):
        """Assistant message content is the spec summary."""
        spec = _sample_spec()
        ctx = _build_ctx(llm_response=spec)
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assistant_msg = result.update["messages"][0]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == "Hello world API"

    async def test_model_router_consulted_for_po(self):
        """The model router is queried with role='po'."""
        ctx = _build_ctx(llm_response=_sample_spec())
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            await node(_base_state())

        ctx.model_router.get_model_for_call.assert_called_once_with("po")


class TestPONodeFailure:
    """Tests for the PO node's error handling — dead-loop prevention."""

    async def test_llm_exception_returns_fallback_command(self):
        """When LLM raises, a fallback Command is returned (not re-raised).

        Critical: the fallback MUST write a placeholder product_spec so
        _force_route (requirements && !product_spec → po) does not
        dead-loop. The placeholder has fallback=True marker.
        """
        ctx = _build_ctx(llm_exc=RuntimeError("LLM is down"))
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assert result.goto == "supervisor"
        assert result.update["status"] == "specing"
        # Fallback spec is a list with one dict marked fallback=True
        fallback_spec = result.update["product_spec"]
        assert len(fallback_spec) == 1
        assert fallback_spec[0]["fallback"] is True
        assert "PO unavailable" in fallback_spec[0]["summary"]
        # Errors list contains the exception
        errors = result.update["errors"]
        assert len(errors) == 1
        assert errors[0]["agent_name"] == "po"
        assert "LLM is down" in errors[0]["content"]
        assert errors[0]["severity"] == "high"

    async def test_fallback_spec_advances_past_po_guard(self):
        """Fallback spec must be non-empty so supervisor's PO guard treats it as 'present'.

        This is the dead-loop prevention contract: PO failure → fallback
        spec → supervisor _force_route sees product_spec non-empty →
        planner (with raw requirements).
        """
        ctx = _build_ctx(llm_exc=ValueError("bad response"))
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        # The product_spec list must be truthy (non-empty) for the guard
        product_spec = result.update["product_spec"]
        assert bool(product_spec), "fallback spec must be non-empty to break dead-loop"
        assert product_spec[0]["fallback"] is True

    async def test_error_message_in_assistant_content(self):
        """The error message is also surfaced in the assistant message."""
        ctx = _build_ctx(llm_exc=ValueError("bad response"))
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        assistant_msg = result.update["messages"][0]
        assert "PO error" in assistant_msg["content"]
        assert "bad response" in assistant_msg["content"]


class TestPONodeContextPropagation:
    """Tests verifying set_llm_context wraps the LLM call."""

    async def test_set_llm_context_entered_for_po(self):
        """The LLM call is wrapped in set_llm_context('po', 'specing')."""
        ctx = _build_ctx(llm_response=_sample_spec())
        node = make_po_node(ctx)

        from contextlib import contextmanager

        from app.services.llm.usage_tracker import set_llm_context

        captured: list[tuple[str, str]] = []
        original_set = set_llm_context

        @contextmanager
        def spy_context(agent_name: str, task_type: str = "general"):
            captured.append((agent_name, task_type))
            with original_set(agent_name, task_type):
                yield

        with (
            patch("app.core.langgraph.agents.context.set_llm_context", spy_context),
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            await node(_base_state())

        assert captured == [("po", "specing")]


class TestPONodeRiskGate:
    """Tests for PO risk gate — human_approval_needed triggers."""

    @staticmethod
    def _spec_with_clarification() -> ProductSpec:
        """Spec that flags needs_clarification=True."""
        base = _sample_spec()
        base.needs_clarification = True
        base.open_questions = ["What authentication method is required?"]
        return base

    @staticmethod
    def _spec_with_low_value() -> ProductSpec:
        """Spec that flags low_value_rationale."""
        base = _sample_spec()
        base.low_value_rationale = "Task is trivial — a single print statement. Dev cost exceeds value."
        return base

    async def test_needs_clarification_triggers_approval_gate(self):
        """needs_clarification=True → human_approval_needed set in update."""
        ctx = _build_ctx(llm_response=self._spec_with_clarification())
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        update = result.update
        assert update.get("human_approval_needed") is True
        assert update["product_spec"][0]["needs_clarification"] is True

    async def test_low_value_rationale_triggers_approval_gate(self):
        """low_value_rationale non-empty → human_approval_needed set in update."""
        ctx = _build_ctx(llm_response=self._spec_with_low_value())
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        update = result.update
        assert update.get("human_approval_needed") is True
        assert update["product_spec"][0]["low_value_rationale"] is not None

    async def test_no_risk_flags_does_not_trigger_gate(self):
        """needs_clarification=False + low_value_rationale=None → no approval gate."""
        ctx = _build_ctx(llm_response=_sample_spec())
        node = make_po_node(ctx)

        with (
            patch("app.core.langgraph.agents.po_node.load_agent_prompt", return_value="sys"),
            patch("app.core.langgraph.agents.po_node.prepare_sdlc_messages", return_value=[]),
            patch("app.core.langgraph.agents.context.dump_sdlc_messages", return_value="[]"),
        ):
            result = await node(_base_state())

        update = result.update
        assert "human_approval_needed" not in update or update.get("human_approval_needed") is False

