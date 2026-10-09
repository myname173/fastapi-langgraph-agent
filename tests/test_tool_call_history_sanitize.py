"""Regression tests for ``sanitize_tool_call_history``.

Context: DeepSeek (and OpenAI-compatible endpoints generally) reject a chat
request when a ``tool`` message is not preceded by an assistant message that
declares a matching ``tool_calls`` id:

    Messages with role 'tool' must be a response to a preceding message with
    'tool_calls'

The SDLC graph could produce exactly that shape — the tester node forced a
``run_tests`` call and tagged the result with the synthetic id
``"auto_run_tests"``, which no assistant message ever declared. That made the
supervisor's LLM call 400 in a retry loop until the workflow stalled.
"""

# ``app.schemas`` must be imported before ``app.utils``: ``app.utils.graph``
# imports ``app.schemas.chat``, which triggers ``app.schemas.__init__`` →
# ``app.schemas.graph`` → ``app.utils.graph``. Importing ``app.utils`` first
# therefore hits a partially-initialised module and fails on
# ``code_changes_reducer``. Loading the schemas package up front breaks the cycle.
import app.schemas  # noqa: F401
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, convert_to_messages

from app.utils import prepare_sdlc_messages
from app.utils.graph import sanitize_tool_call_history


def _ai_with_tool_calls(*ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": cid, "name": "run_tests", "args": {"path": "."}} for cid in ids],
    )


class TestSanitizeToolCallHistory:
    """Tool-call pairing normalisation for OpenAI-compatible providers."""

    def test_drops_orphan_tool_message(self):
        """A tool result with no declaring assistant turn is removed."""
        messages = [
            HumanMessage(content="write a prime checker"),
            _ai_with_tool_calls("c1"),
            ToolMessage(content="listed files", tool_call_id="c1"),
            # Synthetic result: never declared by any assistant message.
            ToolMessage(content="auto run output", tool_call_id="auto_run_tests"),
        ]

        cleaned = sanitize_tool_call_history(messages)

        assert [type(m) for m in cleaned] == [HumanMessage, AIMessage, ToolMessage]
        assert [m.tool_call_id for m in cleaned if isinstance(m, ToolMessage)] == ["c1"]

    def test_keeps_properly_paired_tool_messages(self):
        """A well-formed assistant/tool exchange is untouched."""
        messages = [
            HumanMessage(content="run the suite"),
            _ai_with_tool_calls("c1", "c2"),
            ToolMessage(content="a", tool_call_id="c1"),
            ToolMessage(content="b", tool_call_id="c2"),
        ]

        cleaned = sanitize_tool_call_history(messages)

        assert cleaned == messages

    def test_drops_orphan_left_by_trim_boundary(self):
        """A tool result whose declaring assistant was trimmed away is removed."""
        messages = [
            # The declaring AIMessage is gone — only the tool result survives.
            ToolMessage(content="orphaned", tool_call_id="c1"),
            HumanMessage(content="continue"),
        ]

        cleaned = sanitize_tool_call_history(messages)

        assert [type(m) for m in cleaned] == [HumanMessage]

    def test_strips_unanswered_tool_calls_from_assistant(self):
        """Declared-but-never-answered tool_calls are stripped when content remains."""
        messages = [
            HumanMessage(content="run the suite"),
            AIMessage(
                content="Calling the test runner.",
                tool_calls=[{"id": "c1", "name": "run_tests", "args": {}}],
            ),
            # No tool result follows — the next turn is another assistant message.
            AIMessage(content="done"),
        ]

        cleaned = sanitize_tool_call_history(messages)

        assert len(cleaned) == 3
        assert cleaned[1].tool_calls == []
        assert cleaned[1].content == "Calling the test runner."

    def test_drops_empty_assistant_shell_left_by_stripping(self):
        """An assistant message with no content and only unanswered calls is dropped."""
        messages = [
            HumanMessage(content="go"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "run_tests", "args": {}}]),
            AIMessage(content="done"),
        ]

        cleaned = sanitize_tool_call_history(messages)

        assert [m.content for m in cleaned] == ["go", "done"]

    def test_handles_dict_messages_from_checkpoint(self):
        """Checkpointed histories are plain dicts; sanitising must handle them."""
        messages = [
            {"type": "human", "content": "go"},
            {"type": "ai", "content": "", "tool_calls": [{"id": "c1", "name": "t", "args": {}}]},
            {"type": "tool", "content": "ok", "tool_call_id": "c1"},
            {"type": "tool", "content": "orphan", "tool_call_id": "nope"},
        ]

        cleaned = sanitize_tool_call_history(messages)

        assert len(cleaned) == 3
        assert cleaned[-1]["tool_call_id"] == "c1"

    def test_empty_history_is_returned_as_is(self):
        """An empty history short-circuits without error."""
        assert sanitize_tool_call_history([]) == []

    def test_prepare_sdlc_messages_sanitises_before_returning(self):
        """prepare_sdlc_messages must never hand an orphan tool result to the API."""
        messages = [
            {"type": "human", "content": "go"},
            {"type": "ai", "content": "", "tool_calls": [{"id": "c1", "name": "t", "args": {}}]},
            {"type": "tool", "content": "ok", "tool_call_id": "c1"},
            {"type": "tool", "content": "orphan", "tool_call_id": "auto_run_tests"},
        ]

        prepared = prepare_sdlc_messages(messages, "SYSTEM")

        assert prepared[0] == {"role": "system", "content": "SYSTEM"}
        # trim_messages normalises the dicts into BaseMessage objects.
        tool_ids = [m.tool_call_id for m in convert_to_messages(prepared) if m.type == "tool"]
        assert tool_ids == ["c1"]
