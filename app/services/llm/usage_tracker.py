"""LLM usage tracking via LangChain CallbackHandler.

Records token usage and cost metrics for every LLM call, including
structured-output paths where the ``BaseMessage`` is not directly
accessible to the caller (e.g. ``llm_service.call(response_format=...)``
returns a Pydantic instance).

Architecture
------------

1. :class:`LLMUsageCallbackHandler` is bound to every ``ChatOpenAI``
   instance in :mod:`app.services.llm.registry`.  LangChain invokes it
   on every ``ainvoke`` / ``invoke`` call.
2. The current ``agent_name`` and ``task_type`` are propagated via a
   :class:`contextvars.ContextVar`, so the callback knows which agent
   triggered the call.  When no context is set (e.g. ad-hoc LLM calls
   outside SDLC), ``agent_name="unknown"`` is used.
3. ``on_llm_end`` extracts ``token_usage`` from ``LLMResult.llm_output``
   and records Prometheus metrics via :func:`record_llm_usage`.

This design covers both LLM call paths in the codebase:

- ``llm_service.call(..., response_format=XxxOutput)`` — Pydantic output,
  the caller never sees the ``BaseMessage``, but the callback still fires.
- ``get_llm_for(...).bind_tools(...).ainvoke(...)`` — ``BaseMessage``
  output, callback fires here too.

Concurrency
-----------

``contextvars.ContextVar`` propagates correctly across ``await``
boundaries within the same asyncio task, and is isolated between
concurrent tasks (each task gets its own copy).  This means two
concurrent SDLC workflows won't stomp on each other's ``agent_name``.
"""

import contextlib
import contextvars
from typing import Any, Dict, Optional, Tuple, override

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from app.core.logging import logger
from app.core.metrics import (
    sdlc_llm_cost_usd_total,
    sdlc_llm_token_usage_total,
    sdlc_token_usage_total,
)
from app.services.llm.pricing import calculate_cost_usd

# ContextVar tracks the current agent + task_type for the in-flight LLM call.
# Default ("unknown") covers non-SDLC paths (chatbot, session naming, etc.)
# that don't wrap their calls in ``set_llm_context``.
_current_llm_context: contextvars.ContextVar[Tuple[str, str]] = contextvars.ContextVar(
    "_current_llm_context", default=("unknown", "general")
)


@contextlib.contextmanager
def set_llm_context(agent_name: str, task_type: str = "general"):
    """Set the current agent_name + task_type for LLM usage tracking.

    Wrap any LLM call (sync or async) with this context manager so the
    :class:`LLMUsageCallbackHandler` knows which agent triggered the call.

    Example::

        with set_llm_context("planner", "planning"):
            response = await ctx.llm_service.call(...)

    The context is automatically cleared on exit, even if the wrapped
    code raises.  Concurrent asyncio tasks each get their own context
    (ContextVar semantics), so two parallel workflows won't interfere.

    Args:
        agent_name: Name of the agent making the call (e.g. ``"planner"``).
        task_type: Type of task (e.g. ``"planning"``, ``"coding"``).
    """
    token = _current_llm_context.set((agent_name, task_type))
    try:
        yield
    finally:
        _current_llm_context.reset(token)


def _extract_token_usage(response: LLMResult) -> Optional[Dict[str, int]]:
    """Extract token usage dict from a LangChain ``LLMResult``.

    Handles multiple provider formats:
    - OpenAI / Bailian: ``llm_output["token_usage"]``
    - Some providers: ``llm_output["usage"]``

    Args:
        response: LangChain ``LLMResult`` from ``on_llm_end``.

    Returns:
        Dict with ``prompt_tokens``, ``completion_tokens``, ``total_tokens``
        keys (all ``int``), or ``None`` when no usage info is available.
    """
    llm_output = getattr(response, "llm_output", None)
    if not isinstance(llm_output, dict):
        return None

    # Try OpenAI-style "token_usage" first, then fall back to "usage".
    usage = llm_output.get("token_usage") or llm_output.get("usage")
    if not isinstance(usage, dict):
        return None

    prompt_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    completion_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)

    if prompt_tokens <= 0 and completion_tokens <= 0:
        return None

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _extract_model_name(response: LLMResult, default: str = "unknown") -> str:
    """Extract the model name from a LangChain ``LLMResult``.

    Args:
        response: LangChain ``LLMResult``.
        default: Fallback when the model name is unavailable.

    Returns:
        Model name string, or ``default`` if not found.
    """
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        # OpenAI / Bailian: "model_name" or "model"
        model = llm_output.get("model_name") or llm_output.get("model")
        if isinstance(model, str) and model:
            return model
    return default


def record_llm_usage(
    prompt_tokens: int,
    completion_tokens: int,
    agent_name: str,
    model_name: str,
    task_type: str = "general",
) -> None:
    """Record LLM token usage and cost to Prometheus metrics.

    Safe to call with zero tokens — silently returns without recording.
    All Prometheus operations are idempotent and thread-safe.

    Args:
        prompt_tokens: Number of input tokens consumed.
        completion_tokens: Number of output tokens consumed.
        agent_name: Name of the agent that made the call.
        model_name: Name of the model used (for pricing lookup).
        task_type: Type of task (e.g. ``"planning"``, ``"coding"``).
    """
    total = prompt_tokens + completion_tokens
    if total <= 0:
        return

    # Token counters — activate the existing ``sdlc_token_usage_total``
    # Counter (previously defined but never incremented) and add a finer
    # ``sdlc_llm_token_usage_total`` that splits prompt vs completion.
    sdlc_token_usage_total.labels(agent_name=agent_name, model=model_name).inc(total)

    sdlc_llm_token_usage_total.labels(agent_name=agent_name, model=model_name, token_type="prompt").inc(prompt_tokens)
    sdlc_llm_token_usage_total.labels(agent_name=agent_name, model=model_name, token_type="completion").inc(
        completion_tokens
    )

    # Cost in USD — multiplied by per-model unit price.
    cost = calculate_cost_usd(model_name, prompt_tokens, completion_tokens)
    sdlc_llm_cost_usd_total.labels(agent_name=agent_name, model=model_name, task_type=task_type).inc(cost)

    logger.debug(
        "llm_usage_recorded",
        agent_name=agent_name,
        model=model_name,
        task_type=task_type,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=round(cost, 6),
    )


class LLMUsageCallbackHandler(AsyncCallbackHandler):
    """LangChain callback handler that records LLM token usage and cost.

    Bound to every ``ChatOpenAI`` instance in
    :mod:`app.services.llm.registry`.  Fires on ``on_llm_end`` for both
    sync and async invocations.

    The current ``agent_name`` / ``task_type`` are read from the
    :data:`_current_llm_context` ContextVar, so callers must wrap their
    LLM calls in :func:`set_llm_context` for accurate per-agent attribution.

    All exceptions are caught and logged — metric recording must NEVER
    interrupt an in-flight LLM call.
    """

    @override
    async def on_llm_end(
        self,
        response: LLMResult,
        **kwargs: Any,
    ) -> None:
        """Extract token usage from the LLM result and record metrics.

        Args:
            response: LangChain ``LLMResult`` containing the raw model output.
            **kwargs: Additional LangChain callback kwargs (unused).
        """
        try:
            usage = _extract_token_usage(response)
            if usage is None:
                return

            agent_name, task_type = _current_llm_context.get()
            model_name = _extract_model_name(response)

            record_llm_usage(
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                agent_name=agent_name,
                model_name=model_name,
                task_type=task_type,
            )
        except Exception as e:
            # Metric recording must never break LLM calls.
            logger.warning(
                "llm_usage_record_failed",
                error_type=type(e).__name__,
                error=str(e),
            )


# Module-level singleton — bound to every LLM instance in LLMRegistry.
usage_callback_handler: LLMUsageCallbackHandler = LLMUsageCallbackHandler()
