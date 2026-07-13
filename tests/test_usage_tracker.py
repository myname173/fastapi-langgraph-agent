"""Unit tests for LLM usage tracker (contextvar + callback handler).

Covers:
- set_llm_context context manager (set / reset / nesting)
- _extract_token_usage for OpenAI / Bailian / missing formats
- _extract_model_name
- record_llm_usage increments Prometheus counters correctly
- LLMUsageCallbackHandler.on_llm_end records usage
- Exception safety: handler never raises
"""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.outputs import LLMResult

from app.services.llm.usage_tracker import (
    LLMUsageCallbackHandler,
    _current_llm_context,
    _extract_model_name,
    _extract_token_usage,
    record_llm_usage,
    set_llm_context,
    usage_callback_handler,
)


class TestSetLlmContext:
    """Tests for set_llm_context context manager."""

    def test_default_context_is_unknown(self):
        """Without set_llm_context, the default agent is 'unknown'."""
        agent_name, task_type = _current_llm_context.get()
        assert agent_name == "unknown"
        assert task_type == "general"

    def test_context_sets_and_resets(self):
        """set_llm_context sets the value inside, resets on exit."""
        assert _current_llm_context.get() == ("unknown", "general")

        with set_llm_context("planner", "planning"):
            assert _current_llm_context.get() == ("planner", "planning")

        # Reset to default after exit
        assert _current_llm_context.get() == ("unknown", "general")

    def test_context_default_task_type(self):
        """task_type defaults to 'general' when omitted."""
        with set_llm_context("coder"):
            assert _current_llm_context.get() == ("coder", "general")

    def test_nested_contexts_restore_previous(self):
        """Nested set_llm_context restores the outer value, not the default."""
        with set_llm_context("supervisor", "routing"):
            with set_llm_context("planner", "planning"):
                assert _current_llm_context.get() == ("planner", "planning")
            # Inner exit → back to supervisor
            assert _current_llm_context.get() == ("supervisor", "routing")
        # Outer exit → back to default
        assert _current_llm_context.get() == ("unknown", "general")

    def test_context_resets_even_on_exception(self):
        """Context is reset even when the wrapped block raises."""
        with pytest.raises(RuntimeError, match="boom"):
            with set_llm_context("tester", "testing"):
                raise RuntimeError("boom")

        assert _current_llm_context.get() == ("unknown", "general")


class TestExtractTokenUsage:
    """Tests for _extract_token_usage()."""

    def test_openai_format(self):
        """OpenAI-style token_usage dict is parsed correctly."""
        result = LLMResult(
            generations=[],
            llm_output={
                "token_usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                }
            },
        )
        usage = _extract_token_usage(result)
        assert usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    def test_bailian_format(self):
        """Bailian/DashScope uses the same token_usage key as OpenAI."""
        result = LLMResult(
            generations=[],
            llm_output={
                "token_usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 100,
                }
            },
        )
        usage = _extract_token_usage(result)
        assert usage is not None
        assert usage["prompt_tokens"] == 200
        assert usage["completion_tokens"] == 100
        assert usage["total_tokens"] == 300  # computed

    def test_usage_key_fallback(self):
        """Some providers use 'usage' instead of 'token_usage'."""
        result = LLMResult(
            generations=[],
            llm_output={
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                }
            },
        )
        usage = _extract_token_usage(result)
        assert usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_input_output_token_aliases(self):
        """Some providers use input_tokens / output_tokens instead."""
        result = LLMResult(
            generations=[],
            llm_output={
                "token_usage": {
                    "input_tokens": 80,
                    "output_tokens": 40,
                }
            },
        )
        usage = _extract_token_usage(result)
        assert usage is not None
        assert usage["prompt_tokens"] == 80
        assert usage["completion_tokens"] == 40
        assert usage["total_tokens"] == 120

    def test_missing_llm_output_returns_none(self):
        """LLMResult without llm_output returns None."""
        result = LLMResult(generations=[], llm_output=None)
        assert _extract_token_usage(result) is None

    def test_non_dict_llm_output_returns_none(self):
        """Non-dict llm_output returns None.

        ``LLMResult`` pydantic validation rejects non-dict values, so we
        use a plain object to simulate a malformed response from a
        custom LLM provider.
        """
        result = MagicMock()
        result.llm_output = "not a dict"
        assert _extract_token_usage(result) is None

    def test_missing_usage_key_returns_none(self):
        """llm_output without any usage key returns None."""
        result = LLMResult(generations=[], llm_output={"model_name": "qwen3.7-plus"})
        assert _extract_token_usage(result) is None

    def test_zero_tokens_returns_none(self):
        """Both token counts zero → None (nothing to record)."""
        result = LLMResult(
            generations=[],
            llm_output={
                "token_usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }
            },
        )
        assert _extract_token_usage(result) is None

    def test_negative_tokens_treated_as_zero(self):
        """Negative tokens are coerced to 0, then short-circuited."""
        result = LLMResult(
            generations=[],
            llm_output={
                "token_usage": {
                    "prompt_tokens": -10,
                    "completion_tokens": -5,
                }
            },
        )
        # Both <= 0 → returns None
        assert _extract_token_usage(result) is None


class TestExtractModelName:
    """Tests for _extract_model_name()."""

    def test_model_name_key(self):
        """'model_name' key is read first."""
        result = LLMResult(generations=[], llm_output={"model_name": "qwen3.7-plus"})
        assert _extract_model_name(result) == "qwen3.7-plus"

    def test_model_key_fallback(self):
        """'model' key is used when 'model_name' is absent."""
        result = LLMResult(generations=[], llm_output={"model": "qwen3-coder-plus"})
        assert _extract_model_name(result) == "qwen3-coder-plus"

    def test_model_name_takes_precedence_over_model(self):
        """'model_name' wins over 'model' when both are present."""
        result = LLMResult(
            generations=[],
            llm_output={
                "model_name": "qwen3.7-max-preview",
                "model": "qwen3.7-plus",
            },
        )
        assert _extract_model_name(result) == "qwen3.7-max-preview"

    def test_missing_model_returns_default(self):
        """No model info → returns the default string."""
        result = LLMResult(generations=[], llm_output={"token_usage": {}})
        assert _extract_model_name(result) == "unknown"

    def test_custom_default(self):
        """Caller can pass a custom default."""
        result = LLMResult(generations=[], llm_output={})
        assert _extract_model_name(result, "fallback-model") == "fallback-model"

    def test_empty_string_model_falls_back(self):
        """Empty model string is treated as missing."""
        result = LLMResult(generations=[], llm_output={"model_name": ""})
        assert _extract_model_name(result) == "unknown"

    def test_non_string_model_ignored(self):
        """Non-string model value is ignored, falls back to default."""
        result = LLMResult(generations=[], llm_output={"model_name": 123})
        assert _extract_model_name(result) == "unknown"


class TestRecordLlmUsage:
    """Tests for record_llm_usage() — Prometheus counter increments."""

    def _get_counter_value(self, name: str, labels: dict[str, str]) -> float:
        """Read a labeled Counter sample from the default REGISTRY."""
        from prometheus_client import REGISTRY

        return REGISTRY.get_sample_value(name, labels) or 0.0

    def test_zero_tokens_skips_recording(self):
        """Both token counts zero → no metric increment."""
        labels = {
            "agent_name": "zero_test",
            "model": "qwen3.7-plus",
            "task_type": "planning",
        }
        before = self._get_counter_value("sdlc_llm_cost_usd_total", labels)
        record_llm_usage(0, 0, "zero_test", "qwen3.7-plus", "planning")
        after = self._get_counter_value("sdlc_llm_cost_usd_total", labels)
        assert before == after

    def test_positive_tokens_increments_counters(self):
        """Non-zero tokens increment all three counters."""
        agent = "pos_test_agent"
        labels_token = {"agent_name": agent, "model": "qwen3.7-plus"}
        labels_cost = {"agent_name": agent, "model": "qwen3.7-plus", "task_type": "test"}

        before_token = self._get_counter_value("sdlc_token_usage_total", labels_token)
        before_cost = self._get_counter_value("sdlc_llm_cost_usd_total", labels_cost)

        record_llm_usage(100, 50, agent, "qwen3.7-plus", "test")

        after_token = self._get_counter_value("sdlc_token_usage_total", labels_token)
        after_cost = self._get_counter_value("sdlc_llm_cost_usd_total", labels_cost)

        # Token counter increments by total (100 + 50 = 150)
        assert after_token - before_token == 150
        # Cost counter increments by calculated cost (> 0)
        assert after_cost > before_cost

    def test_prompt_and_completion_split_correctly(self):
        """sdlc_llm_token_usage_total splits prompt vs completion tokens."""
        agent = "split_test"
        labels_prompt = {"agent_name": agent, "model": "qwen3.7-plus", "token_type": "prompt"}
        labels_completion = {
            "agent_name": agent,
            "model": "qwen3.7-plus",
            "token_type": "completion",
        }

        before_prompt = self._get_counter_value("sdlc_llm_token_usage_total", labels_prompt)
        before_completion = self._get_counter_value("sdlc_llm_token_usage_total", labels_completion)

        record_llm_usage(70, 30, agent, "qwen3.7-plus", "test")

        after_prompt = self._get_counter_value("sdlc_llm_token_usage_total", labels_prompt)
        after_completion = self._get_counter_value("sdlc_llm_token_usage_total", labels_completion)

        assert after_prompt - before_prompt == 70
        assert after_completion - before_completion == 30

    def test_default_task_type_is_general(self):
        """task_type defaults to 'general' when omitted."""
        agent = "default_task_agent"
        labels = {"agent_name": agent, "model": "qwen3.7-plus", "task_type": "general"}

        before = self._get_counter_value("sdlc_llm_cost_usd_total", labels)
        record_llm_usage(10, 5, agent, "qwen3.7-plus")
        after = self._get_counter_value("sdlc_llm_cost_usd_total", labels)

        assert after > before


class TestLLMUsageCallbackHandler:
    """Tests for LLMUsageCallbackHandler.on_llm_end()."""

    async def test_handler_records_usage_with_context(self):
        """Handler records usage when context is set and LLMResult has usage."""
        from prometheus_client import REGISTRY

        handler = LLMUsageCallbackHandler()
        labels = {"agent_name": "handler_test", "model": "qwen3.7-plus", "task_type": "testing"}

        before_cost = REGISTRY.get_sample_value("sdlc_llm_cost_usd_total", labels) or 0.0

        with set_llm_context("handler_test", "testing"):
            result = LLMResult(
                generations=[],
                llm_output={
                    "token_usage": {"prompt_tokens": 50, "completion_tokens": 25},
                    "model_name": "qwen3.7-plus",
                },
            )
            await handler.on_llm_end(result)

        after_cost = REGISTRY.get_sample_value("sdlc_llm_cost_usd_total", labels) or 0.0

        assert after_cost > before_cost

    async def test_handler_no_usage_does_nothing(self):
        """Handler silently returns when LLMResult has no usage info."""
        handler = LLMUsageCallbackHandler()
        result = LLMResult(generations=[], llm_output=None)
        # Should not raise
        await handler.on_llm_end(result)

    async def test_handler_never_raises_on_exception(self):
        """Handler catches all exceptions — must never break LLM calls."""
        handler = LLMUsageCallbackHandler()

        # Force an exception inside record_llm_usage by patching _extract_token_usage
        with patch(
            "app.services.llm.usage_tracker._extract_token_usage",
            side_effect=RuntimeError("forced failure"),
        ):
            result = LLMResult(generations=[], llm_output={"token_usage": {"prompt_tokens": 10}})
            # Must NOT raise
            await handler.on_llm_end(result)

    def test_module_singleton_exists(self):
        """usage_callback_handler is a module-level singleton instance."""
        assert isinstance(usage_callback_handler, LLMUsageCallbackHandler)
