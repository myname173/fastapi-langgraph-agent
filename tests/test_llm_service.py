"""Unit tests for LLMService — retries, circular fallback, structured output.

Covers:
- _extract_cache_query helper (string / list / dict message formats)
- Default-path call (no overrides) succeeds on first model
- Circular fallback: first model fails → second model succeeds
- All models fail → RuntimeError
- model_name override with unknown model → ValueError
- response_format triggers structured-output path
- get_llm / get_llm_for return the expected instances
- bind_tools chains and returns self

The LLMRegistry is mocked so no real ChatOpenAI instances are constructed
and no network I/O occurs.
"""

from typing import Any, Type
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from openai import APIError
from pydantic import BaseModel

from app.core.semantic_cache import semantic_cache
from app.services.llm.service import LLMService, _extract_cache_query


class TestExtractCacheQuery:
    """Tests for _extract_cache_query()."""

    def test_plain_string_returned_as_is(self):
        """A plain string input is returned directly."""
        assert _extract_cache_query("hello world") == "hello world"

    def test_list_with_user_dict(self):
        """List of dicts: last user message content is returned."""
        messages = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "what is 1+1"},
        ]
        assert _extract_cache_query(messages) == "what is 1+1"

    def test_list_with_multiple_users_returns_last(self):
        """When multiple user messages exist, the last one is returned."""
        messages = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second question"},
        ]
        assert _extract_cache_query(messages) == "second question"

    def test_list_without_user_message_returns_none(self):
        """No user message in the list → None."""
        messages = [{"role": "system", "content": "system prompt"}]
        assert _extract_cache_query(messages) is None

    def test_empty_list_returns_none(self):
        """Empty list → None."""
        assert _extract_cache_query([]) is None

    def test_empty_user_content_returns_none(self):
        """User message with empty content → None."""
        messages = [{"role": "user", "content": ""}]
        assert _extract_cache_query(messages) is None

    def test_non_string_non_list_returns_none(self):
        """Unsupported input types → None."""
        assert _extract_cache_query(None) is None  # type: ignore[arg-type]
        assert _extract_cache_query(42) is None  # type: ignore[arg-type]

    def test_dict_with_non_string_content_coerced(self):
        """Non-string content is coerced to string."""
        messages = [{"role": "user", "content": 123}]
        assert _extract_cache_query(messages) == "123"


class FakeRunnable:
    """A minimal mock for a LangChain Runnable (model or structured-output chain)."""

    def __init__(self, name: str, response: Any = None, exc: Exception | None = None) -> None:
        """Initialize the fake runnable with a response or exception."""
        self.name = name
        self._response = response
        self._exc = exc
        self.ainvoke = AsyncMock(side_effect=self._invoke)

    async def _invoke(self, _messages: Any) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._response

    def with_structured_output(self, _schema: Type[BaseModel]) -> "FakeRunnable":
        """Return self — the same fake will produce the structured response."""
        return self

    def bind_tools(self, _tools: list) -> "FakeRunnable":
        """Return self — tools are ignored in tests."""
        return self


def _build_mock_registry(model_names: list[str]) -> tuple[MagicMock, list[MagicMock]]:
    """Build a mock LLMRegistry with the given model names.

    Returns (mock_class, list_of_model_entry_dicts).
    """
    entries = [{"name": n, "llm": MagicMock()} for n in model_names]
    mock = MagicMock()
    mock.LLMS = entries
    mock.get_all_names.return_value = model_names
    mock.get_all_names.return_value = model_names
    return mock, entries


class TestLLMServiceCallSuccess:
    """Tests for the happy path of LLMService.call()."""

    async def test_default_path_returns_base_message(self):
        """Default call (no overrides) returns the AIMessage from the model."""
        expected_response = AIMessage(content="hello from LLM")
        fake_runnable = FakeRunnable("qwen3.7-plus", response=expected_response)

        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [{"name": "qwen3.7-plus", "llm": MagicMock()}]
            mock_registry.get_all_names.return_value = ["qwen3.7-plus"]
            mock_registry.get.return_value = fake_runnable

            service = LLMService()
            # Bypass retry decorator's exponential backoff for test speed
            with patch.object(service, "_invoke_with_retry", new=AsyncMock(return_value=expected_response)):
                result = await service.call([{"role": "user", "content": "hi"}])

        assert isinstance(result, BaseMessage)
        assert result.content == "hello from LLM"

    async def test_response_format_returns_pydantic_instance(self):
        """response_format triggers with_structured_output and returns BaseModel."""

        class FakeOutput(BaseModel):
            answer: str

        expected = FakeOutput(answer="42")
        fake_runnable = FakeRunnable("qwen3.7-plus", response=expected)

        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [{"name": "qwen3.7-plus", "llm": MagicMock()}]
            mock_registry.get_all_names.return_value = ["qwen3.7-plus"]
            mock_registry.get.return_value = fake_runnable

            service = LLMService()
            with patch.object(service, "_invoke_with_retry", new=AsyncMock(return_value=expected)):
                result = await service.call(
                    [{"role": "user", "content": "what is 6*7"}],
                    response_format=FakeOutput,
                )

        assert isinstance(result, FakeOutput)
        assert result.answer == "42"


class TestLLMServiceFallback:
    """Tests for circular fallback when the first model fails."""

    async def test_first_model_fails_second_succeeds(self):
        """When the first model raises OpenAIError, fallback to the next."""
        expected = AIMessage(content="from second model")
        # First model raises, second succeeds
        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [
                {"name": "model-a", "llm": MagicMock()},
                {"name": "model-b", "llm": MagicMock()},
            ]
            mock_registry.get_all_names.return_value = ["model-a", "model-b"]

            service = LLMService()
            # Simulate: first call raises APIError, second returns expected
            call_count = [0]

            async def fake_invoke_with_retry(_target, _messages):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise APIError("model-a down", request=MagicMock(), body=None)
                return expected

            with patch.object(service, "_invoke_with_retry", new=fake_invoke_with_retry):
                result = await service.call([{"role": "user", "content": "hi"}])

        assert result == expected
        assert call_count[0] == 2  # Tried model-a then model-b

    async def test_all_models_fail_raises_runtime_error(self):
        """When all models fail, RuntimeError is raised."""
        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [
                {"name": "model-a", "llm": MagicMock()},
                {"name": "model-b", "llm": MagicMock()},
            ]
            mock_registry.get_all_names.return_value = ["model-a", "model-b"]

            service = LLMService()

            async def always_fail(_target, _messages):
                raise APIError("all down", request=MagicMock(), body=None)

            with patch.object(service, "_invoke_with_retry", new=always_fail):
                with pytest.raises(RuntimeError, match="failed to get response from llm"):
                    await service.call([{"role": "user", "content": "hi"}])


class TestLLMServiceModelOverride:
    """Tests for model_name override behavior."""

    async def test_unknown_model_name_raises_value_error(self):
        """model_name not in registry → ValueError."""
        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [{"name": "qwen3.7-plus", "llm": MagicMock()}]
            mock_registry.get_all_names.return_value = ["qwen3.7-plus"]

            service = LLMService()
            with pytest.raises(ValueError, match="not found in registry"):
                await service.call(
                    [{"role": "user", "content": "hi"}],
                    model_name="nonexistent-model",
                )


class TestLLMServiceGetLlm:
    """Tests for get_llm() and get_llm_for()."""

    def test_get_llm_returns_default_instance(self):
        """get_llm() returns the instance set during __init__."""
        fake_llm = MagicMock(name="default-llm")
        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [{"name": "qwen3.7-plus", "llm": fake_llm}]
            mock_registry.get_all_names.return_value = ["qwen3.7-plus"]
            mock_registry.get.return_value = fake_llm

            service = LLMService()
            assert service.get_llm() is fake_llm

    def test_get_llm_for_none_returns_default(self):
        """get_llm_for(None) returns the default instance."""
        fake_llm = MagicMock(name="default-llm")
        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [{"name": "qwen3.7-plus", "llm": fake_llm}]
            mock_registry.get_all_names.return_value = ["qwen3.7-plus"]
            mock_registry.get.return_value = fake_llm

            service = LLMService()
            assert service.get_llm_for(None) is fake_llm

    def test_get_llm_for_specific_model_calls_registry(self):
        """get_llm_for(name) returns a fresh instance from the registry."""
        fake_default = MagicMock(name="default-llm")
        fake_specific = MagicMock(name="specific-llm")
        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [{"name": "qwen3.7-plus", "llm": fake_default}]
            mock_registry.get_all_names.return_value = ["qwen3.7-plus", "qwen3-coder-plus"]
            mock_registry.get.return_value = fake_specific

            service = LLMService()
            result = service.get_llm_for("qwen3-coder-plus")
            assert result is fake_specific
            mock_registry.get.assert_called_with("qwen3-coder-plus")


class TestLLMServiceBindTools:
    """Tests for bind_tools()."""

    def test_bind_tools_returns_self_for_chaining(self):
        """bind_tools returns the service instance for method chaining."""
        fake_llm = MagicMock()
        fake_llm.bind_tools.return_value = fake_llm
        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [{"name": "qwen3.7-plus", "llm": fake_llm}]
            mock_registry.get_all_names.return_value = ["qwen3.7-plus"]
            mock_registry.get.return_value = fake_llm

            service = LLMService()
            tools = [MagicMock()]
            result = service.bind_tools(tools)
            assert result is service
            fake_llm.bind_tools.assert_called_once_with(tools)

    def test_bind_tools_no_op_when_llm_not_initialized(self):
        """bind_tools is a no-op when self._llm is None.

        We bypass ``__init__`` (which assumes at least one model in the
        registry) and directly construct a bare instance to test the
        ``self._llm`` truthiness guard in ``bind_tools``.
        """
        service = LLMService.__new__(LLMService)
        service._llm = None
        service._bound_tools = []
        # Should not raise — the guard short-circuits when _llm is None
        service.bind_tools([MagicMock()])
        assert service._bound_tools == []


class TestSemanticCacheWithToolsBound:
    """Regression tests for P2.

    The chat agent calls ``llm_service.bind_tools(...)`` on the shared
    singleton during ``LangGraphAgent.__init__``. That used to set
    ``self._bound_tools`` permanently, and the cache guard ``not
    self._bound_tools`` then disabled the semantic cache for *every* chat
    call. Caching must now work regardless of bound tools, while tool-call
    responses (non-deterministic) remain uncached.
    """

    async def _make_service(self) -> LLMService:
        with patch("app.services.llm.service.LLMRegistry") as mock_registry:
            mock_registry.LLMS = [{"name": "qwen3.7-plus", "llm": MagicMock()}]
            mock_registry.get_all_names.return_value = ["qwen3.7-plus"]
            return LLMService()

    async def test_cache_hit_ignores_bound_tools(self):
        """A cache hit is served even when tools are bound (the old bug)."""
        service = await self._make_service()
        # Mirror LangGraphAgent.__init__ mutating the singleton.
        service._bound_tools = [MagicMock()]

        async def fake_get(*_a, **_k):
            return "cached investment advice"

        with (
            patch.object(semantic_cache, "get", side_effect=fake_get),
            patch.object(semantic_cache, "set", new=AsyncMock()),
        ):
            with patch.object(service, "_invoke_with_retry", new=AsyncMock()) as mock_invoke:
                result = await service.call(
                    [{"role": "user", "content": "what is a good strategy"}],
                    cacheable=True,
                    cache_user_id="u1",
                )
        assert isinstance(result, AIMessage)
        assert result.content == "cached investment advice"
        # Served from cache — the LLM must not have been invoked.
        mock_invoke.assert_not_called()

    async def test_cache_miss_writes_when_tools_bound(self):
        """On a miss, the text response is written even with tools bound."""
        service = await self._make_service()
        service._bound_tools = [MagicMock()]

        async def fake_get(*_a, **_k):
            return None  # miss

        captured: dict = {}

        async def fake_set(*_a, **_k):
            captured.update(_k)

        with (
            patch.object(semantic_cache, "get", side_effect=fake_get),
            patch.object(semantic_cache, "set", side_effect=fake_set),
        ):
            with (
                patch.object(service, "_build_target", new=MagicMock()),
                patch.object(
                    service,
                    "_invoke_with_retry",
                    new=AsyncMock(return_value=AIMessage(content="fresh answer")),
                ),
            ):
                result = await service.call(
                    [{"role": "user", "content": "explain compound interest"}],
                    cacheable=True,
                    cache_user_id="u2",
                )
        assert result.content == "fresh answer"
        # Write should have been attempted despite tools being bound.
        assert captured, "expected the response to be written to the cache"

    async def test_tool_call_response_not_cached(self):
        """A response with tool calls is never written to the cache."""
        service = await self._make_service()
        service._bound_tools = [MagicMock()]

        tool_response = AIMessage(
            content="calling tool",
            tool_calls=[{"name": "get_quote", "args": {"symbol": "AAPL"}, "id": "1"}],
        )

        async def fake_get(*_a, **_k):
            return None  # miss

        wrote = {"called": False}

        async def fake_set(*_a, **_k):
            wrote["called"] = True

        with (
            patch.object(semantic_cache, "get", side_effect=fake_get),
            patch.object(semantic_cache, "set", side_effect=fake_set),
        ):
            with (
                patch.object(service, "_build_target", new=MagicMock()),
                patch.object(service, "_invoke_with_retry", new=AsyncMock(return_value=tool_response)),
            ):
                result = await service.call(
                    [{"role": "user", "content": "what is the price of AAPL"}],
                    cacheable=True,
                    cache_user_id="u3",
                )
        assert result.tool_calls  # passthrough to tool-call routing
        assert wrote["called"] is False, "tool-call responses must not be cached"
