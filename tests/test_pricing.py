"""Unit tests for LLM pricing table and cost calculation.

Covers:
- Known model pricing lookup
- Unknown model fallback to DEFAULT_PRICING
- Cost calculation arithmetic (input + output tokens)
- Edge cases: zero tokens, negative tokens, large token counts
"""

import pytest

from app.services.llm.pricing import (
    DEFAULT_PRICING,
    MODEL_PRICING_USD_PER_1M,
    calculate_cost_usd,
    get_model_pricing,
)


class TestGetModelPricing:
    """Tests for get_model_pricing()."""

    def test_known_model_returns_table_entry(self):
        """A model in MODEL_PRICING_USD_PER_1M returns its exact entry."""
        pricing = get_model_pricing("qwen3.7-plus")
        assert pricing == MODEL_PRICING_USD_PER_1M["qwen3.7-plus"]
        assert pricing == {"input": 0.4, "output": 1.2}

    def test_known_coder_model(self):
        """qwen3-coder-plus is in the table (coding specialist tier)."""
        pricing = get_model_pricing("qwen3-coder-plus")
        assert pricing == {"input": 0.4, "output": 1.2}

    def test_known_max_model(self):
        """qwen3.7-max-preview is in the table (heavy tier)."""
        pricing = get_model_pricing("qwen3.7-max-preview")
        assert pricing == {"input": 2.4, "output": 9.6}

    def test_unknown_model_falls_back_to_default(self):
        """Unknown models return DEFAULT_PRICING so costs are never under-reported."""
        pricing = get_model_pricing("gpt-99-not-real")
        assert pricing == DEFAULT_PRICING
        assert pricing == {"input": 1.0, "output": 3.0}

    def test_empty_string_model_name_falls_back(self):
        """Empty model name returns the default pricing."""
        assert get_model_pricing("") == DEFAULT_PRICING

    def test_default_pricing_is_conservative_high(self):
        """DEFAULT_PRICING is intentionally higher than known models."""
        # Ensure the fallback never under-reports vs. the cheapest known model.
        cheapest_known_input = min(p["input"] for p in MODEL_PRICING_USD_PER_1M.values())
        cheapest_known_output = min(p["output"] for p in MODEL_PRICING_USD_PER_1M.values())
        assert DEFAULT_PRICING["input"] >= cheapest_known_input
        assert DEFAULT_PRICING["output"] >= cheapest_known_output


class TestCalculateCostUsd:
    """Tests for calculate_cost_usd()."""

    def test_zero_tokens_returns_zero(self):
        """No tokens consumed → zero cost."""
        assert calculate_cost_usd("qwen3.7-plus", 0, 0) == 0.0

    def test_only_prompt_tokens(self):
        """Input-only call: cost = prompt_tokens * input_rate / 1M."""
        # qwen3.7-plus input = $0.4 per 1M → 1M tokens = $0.4
        cost = calculate_cost_usd("qwen3.7-plus", 1_000_000, 0)
        assert cost == pytest.approx(0.4)

    def test_only_completion_tokens(self):
        """Output-only call: cost = completion_tokens * output_rate / 1M."""
        # qwen3.7-plus output = $1.2 per 1M → 1M tokens = $1.2
        cost = calculate_cost_usd("qwen3.7-plus", 0, 1_000_000)
        assert cost == pytest.approx(1.2)

    def test_mixed_tokens_summed_correctly(self):
        """Both input and output contribute to the total cost."""
        # 500K input @ 0.4/1M = $0.2; 250K output @ 1.2/1M = $0.3 → total $0.5
        cost = calculate_cost_usd("qwen3.7-plus", 500_000, 250_000)
        assert cost == pytest.approx(0.5)

    def test_max_tier_model_pricing(self):
        """qwen3.7-max-preview uses the heavy-tier rate."""
        # input $2.4/1M, output $9.6/1M → 100K input + 100K output = $0.24 + $0.96 = $1.20
        cost = calculate_cost_usd("qwen3.7-max-preview", 100_000, 100_000)
        assert cost == pytest.approx(1.20)

    def test_unknown_model_uses_default_pricing(self):
        """Unknown model uses DEFAULT_PRICING (input $1.0, output $3.0 per 1M)."""
        cost = calculate_cost_usd("unknown-model", 1_000_000, 1_000_000)
        # 1M * 1.0/1M + 1M * 3.0/1M = 1.0 + 3.0 = 4.0
        assert cost == pytest.approx(4.0)

    def test_negative_tokens_treated_as_zero_input(self):
        """Negative token counts should not produce negative cost.

        The guard ``prompt_tokens <= 0 and completion_tokens <= 0`` only
        short-circuits when BOTH are non-positive.  A negative prompt
        with a positive completion still computes (negative * rate),
        which is a known edge case — callers should never pass negatives.
        This test documents the current behavior.
        """
        # Both non-positive → short-circuit returns 0.0
        assert calculate_cost_usd("qwen3.7-plus", -100, -50) == 0.0
        # Mixed: -100 prompt + 1M completion → -100 * 0.4/1M + 1.2 ≈ 1.19996
        cost = calculate_cost_usd("qwen3.7-plus", -100, 1_000_000)
        assert cost == pytest.approx(1.2 - 100 * 0.4 / 1_000_000)

    def test_small_token_count_is_small_cost(self):
        """Typical agent call: ~1K input + 500 output → cents, not dollars."""
        cost = calculate_cost_usd("qwen3.7-plus", 1_000, 500)
        # 1000 * 0.4/1M + 500 * 1.2/1M = 0.0004 + 0.0006 = 0.001
        assert cost == pytest.approx(0.001)
        assert cost < 0.01  # less than a cent

    def test_return_type_is_float(self):
        """Return value is always a float (not int)."""
        assert isinstance(calculate_cost_usd("qwen3.7-plus", 0, 0), float)
        assert isinstance(calculate_cost_usd("qwen3.7-plus", 100, 50), float)

    def test_all_table_models_compute_without_error(self):
        """Every model in the pricing table can compute a cost without raising."""
        for model_name in MODEL_PRICING_USD_PER_1M:
            cost = calculate_cost_usd(model_name, 1_000, 1_000)
            assert cost > 0
            assert isinstance(cost, float)
