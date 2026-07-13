"""Model pricing table for LLM cost tracking.

Prices are in **USD per 1 million tokens**, separated by input/output.
Sources: provider public pricing pages (Bailian/DashScope, OpenAI).
Update quarterly or when a model is added to :data:`router.ROUTING_TABLE`.

The table is intentionally a module-level constant — prices do not change
at runtime.  When a model is missing from the table, a conservative
default is used so costs are never under-reported.

Integration
-----------

This module is consumed by :mod:`app.services.llm.usage_tracker` which
multiplies token counts by these rates to produce ``sdlc_llm_cost_usd_total``
Prometheus metrics.  It has no side effects and no dependencies on the rest
of the codebase, so it can be unit-tested in isolation.
"""

from typing import Dict

# Model name → {input, output} USD per 1M tokens.
# Aligned with the models in ``router.ROUTING_TABLE``.
MODEL_PRICING_USD_PER_1M: Dict[str, Dict[str, float]] = {
    # --- qwen3.7-plus family (medium tier) ---
    "qwen3.7-plus": {"input": 0.4, "output": 1.2},
    # --- qwen3.7-max family (heavy tier) ---
    "qwen3.7-max-preview": {"input": 2.4, "output": 9.6},
    "qwen3.7-max-2026-06-08": {"input": 2.4, "output": 9.6},
    # --- coding specialist ---
    "qwen3-coder-plus": {"input": 0.4, "output": 1.2},
}

# Conservative fallback so unknown models are never under-reported.
# This is intentionally higher than any known model.
DEFAULT_PRICING: Dict[str, float] = {"input": 1.0, "output": 3.0}


def get_model_pricing(model_name: str) -> Dict[str, float]:
    """Return ``{input, output}`` USD-per-1M-tokens rates for a model.

    Falls back to :data:`DEFAULT_PRICING` when the model is not in the
    table, ensuring costs are never silently under-reported.

    Args:
        model_name: Model identifier (e.g. ``"qwen3.7-plus"``).

    Returns:
        Dict with ``input`` and ``output`` keys, values in USD per 1M tokens.
    """
    return MODEL_PRICING_USD_PER_1M.get(model_name, DEFAULT_PRICING)


def calculate_cost_usd(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate the USD cost of a single LLM call.

    Args:
        model_name: Model identifier used to look up pricing.
        prompt_tokens: Number of input tokens consumed.
        completion_tokens: Number of output tokens consumed.

    Returns:
        Total cost in USD (float).  Returns ``0.0`` when both token counts
        are zero so callers can safely call this on empty responses.
    """
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return 0.0
    pricing = get_model_pricing(model_name)
    return prompt_tokens * pricing["input"] / 1_000_000 + completion_tokens * pricing["output"] / 1_000_000
