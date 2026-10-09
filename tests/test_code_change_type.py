"""Regression tests for the ``edit_type`` → ``change_type`` vocabulary split.

Context: ``CodeEdit.edit_type`` allows ``replace`` / ``insert`` / ``create`` /
``delete``, but ``FileChange.change_type`` (the API/UI representation) only
allows ``create`` / ``modify`` / ``delete``. The coder used to copy one into
the other verbatim, so a model that chose ``apply_multi_file_edits`` with
``edit_type="replace"`` produced ``change_type="replace"``, which failed
``SDLCResponse`` validation at serialisation time — the workflow completed and
was persisted as ``completed``, but the HTTP call returned 500.
"""

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.langgraph.agents.coder_node import _normalise_change_type
from app.core.langgraph.agents.context import AgentContext
from app.schemas.sdlc import FileChange


class TestNormaliseChangeType:
    """``_normalise_change_type`` maps the edit vocabulary onto file changes."""

    @pytest.mark.parametrize(
        ("edit_type", "expected"),
        [
            ("create", "create"),
            ("delete", "delete"),
            # replace/insert describe how the bytes changed, not what the user
            # sees — both are modifications of an existing file.
            ("replace", "modify"),
            ("insert", "modify"),
            ("modify", "modify"),
            # Case/whitespace tolerance.
            ("  REPLACE ", "modify"),
            # Unknown values must not leak into the response schema.
            ("rename", "modify"),
            (None, "modify"),
            ("", "modify"),
        ],
    )
    def test_mapping(self, edit_type: object, expected: str) -> None:
        """Every edit type resolves to a valid ``change_type``."""
        assert _normalise_change_type(edit_type) == expected

    def test_output_is_always_a_valid_literal(self) -> None:
        """Whatever the input, the result is accepted by ``FileChange``."""
        for candidate in ("replace", "insert", "create", "delete", "bogus", None):
            change_type = _normalise_change_type(candidate)
            FileChange(
                file_path="a.py",
                change_type=change_type,
                content="x = 1",
            )


class TestFileChangeCoercion:
    """``FileChange`` tolerates the edit vocabulary for stored state."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("replace", "modify"),
            ("insert", "modify"),
            ("REPLACE", "modify"),
            ("create", "create"),
            ("modify", "modify"),
            ("delete", "delete"),
        ],
    )
    def test_known_aliases_are_coerced(self, raw: str, expected: str) -> None:
        """Known edit aliases deserialise instead of raising."""
        change = FileChange(file_path="a.py", change_type=raw, content="x = 1")
        assert change.change_type == expected

    def test_unknown_value_still_rejected(self) -> None:
        """A genuinely invalid value must still fail validation."""
        with pytest.raises(ValidationError):
            FileChange(file_path="a.py", change_type="explode", content="x = 1")


# ---------------------------------------------------------------------------
# Trajectory-level: coder_node must emit schema-valid code_changes.
# ---------------------------------------------------------------------------


def _coder_ctx(llm_response: object, tools: list) -> object:
    """Build an ``AgentContext`` whose LLM returns ``llm_response``.

    Mirrors the mocking style of ``test_coder_node_mcp_trajectory`` — no real
    LLM, no registry, no docker. The coder node routes its LLM call through
    ``llm_service.call(messages, model_name=..., tools=...)`` (so the call is
    audited), so that is what we mock.
    """
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


class TestCoderNodeEmitsValidChangeType:
    """``apply_multi_file_edits`` must not leak ``edit_type`` into ``change_type``.

    This is the exact end-to-end failure: DeepSeek preferred
    ``apply_multi_file_edits`` with ``edit_type="replace"``, the coder copied
    that into ``code_changes[].change_type``, and the API then failed
    ``SDLCResponse`` validation — the workflow was persisted as ``completed``
    but the client got HTTP 500.
    """

    @pytest.mark.asyncio
    async def test_multi_file_edits_produce_valid_change_types(self) -> None:
        """Every emitted change validates against ``FileChange``."""
        from langgraph.graph.state import Command

        from app.core.langgraph.agents.coder_node import make_coder_node

        edit_tool = MagicMock()
        edit_tool.name = "apply_multi_file_edits"
        edit_tool.ainvoke = AsyncMock(return_value="Successfully applied 3 edits")

        ai_response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "apply_multi_file_edits",
                    "args": {
                        "thread_id": "default",
                        "edits": [
                            {
                                "file_path": "is_prime.py",
                                "edit_type": "replace",
                                "old_str": "def is_prime(n):",
                                "new_str": "def is_prime(n: int) -> bool:",
                            },
                            {
                                "file_path": "is_prime.py",
                                "edit_type": "insert",
                                "old_str": "# end",
                                "new_str": "\n# trailing helper\n",
                            },
                            {
                                "file_path": "helper.py",
                                "edit_type": "create",
                                "new_str": "VALUE = 1\n",
                            },
                        ],
                    },
                    "id": "call-edit",
                    "type": "tool_call",
                }
            ],
        )

        ctx = _coder_ctx(ai_response, [edit_tool])
        node = make_coder_node(ctx)

        state = {
            "messages": [{"role": "user", "content": "edit the files"}],
            "requirements": "edit the files",
            "plan": [],
            "code_changes": [],
            "coder_retry_count": 0,
        }

        with (
            patch(
                "app.core.langgraph.agents.coder_node.load_agent_prompt",
                return_value="sys",
            ),
            patch(
                "app.core.langgraph.agents.coder_node.prepare_sdlc_messages",
                return_value=[],
            ),
            patch(
                "app.core.langgraph.agents.coder_node._build_code_index_context",
                AsyncMock(return_value=""),
            ),
        ):
            result = await node(state, config={"configurable": {"thread_id": "th-test"}})

        assert isinstance(result, Command)
        assert result.goto == "supervisor"

        changes = result.update["code_changes"]
        assert len(changes) == 3

        # The load-bearing assertion: nothing here may fail SDLCResponse
        # validation. This is what returned 500 in production.
        for change in changes:
            FileChange(**change)

        by_path = {c["file_path"]: c["change_type"] for c in changes}
        assert by_path["helper.py"] == "create"
        # replace + insert both describe a modification of an existing file.
        assert changes[0]["change_type"] == "modify"
        assert changes[1]["change_type"] == "modify"
