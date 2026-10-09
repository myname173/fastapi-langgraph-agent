"""LLM service with retries, circular fallback, and optional structured output.

Every public ``call`` proceeds through a **per-call local cursor** over the
model registry. This eliminates the race condition that existed when the
previous implementation mutated ``self._llm`` / ``self._current_model_index``
globally mid-flight: concurrent requests can never see each other's model
switches because fallback state is local to each invocation.

Two distinct execution paths:

- **Default path** (no model_name / response_format / model_kwargs): builds
  a per-call target from the registry, binding tools fresh each attempt so
  ``self._llm`` is never mutated.
- **One-off path** (any override provided): resolves a fresh, local
  ``Runnable`` for the call without touching ``self._llm``.
"""

import asyncio
import logging
import time
from typing import (
    Any,
    Callable,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
    overload,
)

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from openai import (
    APIError,
    APITimeoutError,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.logging import logger
from app.core.semantic_cache import semantic_cache
from app.services.llm.registry import LLMRegistry
from app.services.llm.execution_context import get_llm_execution_context
from app.services.llm.policy import model_policy
from app.services.llm_execution_service import llm_execution_service

T = TypeVar("T", bound=BaseModel)


def _extract_cache_query(messages: LanguageModelInput) -> Optional[str]:
    """Extract the last user message content for semantic cache lookup.

    Args:
        messages: LangChain language model input (list of dicts, messages,
            or a plain string).

    Returns:
        The last user message content, or ``None`` if not extractable.
    """
    if isinstance(messages, str):
        return messages
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                return str(content) if content else None
    return None


def _is_tool_call_response(result: Any) -> bool:
    """Return ``True`` when an LLM result contains tool calls.

    Tool-call responses depend on live tool output and are therefore
    non-deterministic across calls. They must never be cached or served
    from the cache, otherwise a stale text answer could be returned for a
    query whose correct answer requires a fresh tool invocation.
    """
    return isinstance(result, AIMessage) and bool(result.tool_calls)


class LLMService:
    """Service for managing LLM calls with retries and circular fallback.

    Concurrency model
    -----------------
    ``self._llm`` and ``self._healthy_index`` are **read-only** after
    initialisation. Per-call fallback walks a *local* cursor starting
    from a snapshot of ``_healthy_index``. Tool bindings are constructed
    fresh per attempt (cheap: ``bind_tools`` returns a lightweight wrapper),
    so no global mutable state is touched during a call — safe under any
    level of concurrency.
    """

    def __init__(self):
        """Initialize the LLM service with the configured default model."""
        self._llm: Any = None  # BaseChatModel; read-only after __init__
        self._healthy_index: int = 0
        # Per-message chat model rotation cursor. INDEPENDENT from
        # _healthy_index: advanced only by next_chat_rotation_model(), never
        # by the fallback loop, so rotation cannot corrupt the default
        # fallback chain (project_memory constraint).
        self._chat_rotation_index: int = 0
        self._bound_tools: List = []

        all_names = LLMRegistry.get_all_names()
        try:
            self._healthy_index = all_names.index(settings.DEFAULT_LLM_MODEL)
            self._llm = LLMRegistry.get(settings.DEFAULT_LLM_MODEL)
            logger.info(
                "llm_service_initialized",
                default_model=settings.DEFAULT_LLM_MODEL,
                model_index=self._healthy_index,
                total_models=len(all_names),
                environment=settings.ENVIRONMENT.value,
            )
        except Exception as e:
            self._healthy_index = 0
            self._llm = LLMRegistry.LLMS[0]["llm"]
            logger.warning(
                "default_model_not_found_using_first",
                requested=settings.DEFAULT_LLM_MODEL,
                using=all_names[0] if all_names else "none",
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @overload
    async def call(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str] = ...,
        response_format: None = ...,
        *,
        cacheable: bool = ...,
        cache_user_id: Optional[str] = ...,
        bind_tools: bool = ...,
        tools: Optional[List] = ...,
        **model_kwargs: Any,
    ) -> BaseMessage: ...

    @overload
    async def call(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str] = ...,
        *,
        response_format: Type[T],
        cacheable: bool = ...,
        cache_user_id: Optional[str] = ...,
        bind_tools: bool = ...,
        tools: Optional[List] = ...,
        **model_kwargs: Any,
    ) -> T: ...

    async def call(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str] = None,
        response_format: Optional[Type[BaseModel]] = None,
        *,
        cacheable: bool = False,
        cache_user_id: Optional[str] = None,
        bind_tools: bool = True,
        tools: Optional[List] = None,
        **model_kwargs: Any,
    ) -> Union[BaseMessage, BaseModel]:
        """Call the LLM with retries and circular fallback.

        Args:
            messages: Conversation messages to send.
            model_name: Override the model. ``None`` uses the current default.
            response_format: Pydantic schema for structured output. When
                provided the call chains ``.with_structured_output(schema)``
                and returns a validated instance of that schema instead of a
                raw ``BaseMessage``.
            cacheable: When ``True`` and no ``response_format`` is used, check
                the semantic cache before calling the LLM.  Whether tools are
                bound to the model does not disable caching — most chat
                answers are plain text and are cached regardless.  On a hit,
                return the cached response as an ``AIMessage`` without making
                an LLM call.  On a miss, store the response after the call
                succeeds, unless the response contains tool calls (which are
                non-deterministic and must not be cached).
            cache_user_id: User ID for per-user cache scoping.  Required when
                ``cacheable`` is ``True`` to avoid cross-user cache pollution.
            bind_tools: When ``True`` (default), bind ``self._bound_tools`` to
                the model so tool calling works. Callers that don't need tools
                (e.g. intent classification, research debate) should pass
                ``False`` to avoid sending tool definitions to models that may
                not support them.
            tools: Explicit tool list to bind for this call, overriding
                ``self._bound_tools``. Agent nodes (coder, tester) need an
                agent-scoped tool subset rather than the global registry, and
                must route through here — **not** through
                ``get_llm_for(...).bind_tools(...).ainvoke()`` — so the call is
                retried, falls back across models, and is written to the
                ``llm_execution`` audit trail.
            **model_kwargs: Extra kwargs forwarded to ``LLMRegistry.get`` when
                constructing a one-off model instance (e.g. ``temperature``,
                ``max_tokens``, ``reasoning``).

        Returns:
            ``BaseMessage`` when ``response_format`` is ``None``, otherwise a
            validated instance of ``response_format``.

        Raises:
            RuntimeError: When all models fail after retries or the total
                timeout budget is exceeded.
        """
        # Check semantic cache for cacheable text-only calls (no structured
        # output). Whether tools are bound to the model is irrelevant here:
        # the chat agent always binds tools, yet most answers are plain text
        # that should be cached. We only skip serving a cached answer when the
        # *response itself* contains tool calls (handled on the write side),
        # because tool-call results are non-deterministic.
        if cacheable and response_format is None:
            query = _extract_cache_query(messages)
            if query:
                cached = await semantic_cache.get(
                    "llm",
                    cache_user_id,
                    query,
                    threshold=settings.SEMANTIC_CACHE_LLM_THRESHOLD,
                )
                if cached is not None:
                    logger.info("llm_semantic_cache_hit", query_length=len(query))
                    return AIMessage(content=cached)

        try:
            result = await asyncio.wait_for(
                self._call_with_fallback(messages, model_name, response_format, model_kwargs, bind_tools, tools),
                timeout=settings.LLM_TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.exception(
                "llm_total_timeout_exceeded",
                timeout_seconds=settings.LLM_TOTAL_TIMEOUT,
            )
            raise RuntimeError(f"llm call timed out after {settings.LLM_TOTAL_TIMEOUT}s total budget")

        # Store successful text-only responses in semantic cache. Skip
        # tool-call responses (non-deterministic) even though tools may be
        # bound to the model for this call.
        if cacheable and response_format is None and not _is_tool_call_response(result):
            if isinstance(result, BaseMessage) and isinstance(result.content, str):
                query = _extract_cache_query(messages)
                if query and result.content:
                    await semantic_cache.set(
                        "llm",
                        cache_user_id,
                        query,
                        result.content,
                        model_name=model_name or settings.DEFAULT_LLM_MODEL,
                    )

        return result

    def get_llm(self) -> Any:
        """Return the current default LLM instance (read-only).

        This is used by callers that need to inspect the model name for
        metrics labels. The returned instance is **never mutated** by
        the fallback logic.

        Returns:
            Current ``BaseChatModel`` instance or ``None`` if not initialised.
        """
        return self._llm

    def get_llm_for(self, model_name: Optional[str] = None) -> Any:
        """Return a specific model instance for direct use (e.g. ``bind_tools``).

        When ``model_name`` is ``None`` or matches the default, returns the
        default instance (same as :meth:`get_llm`).  Otherwise returns a
        fresh instance from the registry for the requested model.

        Used by agent nodes that call ``bind_tools().ainvoke()`` directly
        (coder, tester) instead of going through :meth:`call`.

        Args:
            model_name: Model name from :class:`ModelRouter`, or ``None``
                for the default model.

        Returns:
            ``BaseChatModel`` instance.
        """
        selected_model = model_name or settings.DEFAULT_LLM_MODEL
        model_policy.validate(selected_model, get_llm_execution_context().purpose)
        if model_name is None:
            return self._llm
        return LLMRegistry.get(model_name)

    def next_chat_rotation_model(self) -> Optional[str]:
        """Return the next chat model name in round-robin order.

        Cycles through ``settings.CHAT_ROTATION_MODELS`` independently from
        the healthy-pointer fallback chain. Returns ``None`` when the
        rotation list is empty (no rotation configured).

        asyncio's single-threaded model makes the index advance safe
        without a lock, mirroring :meth:`_advance_healthy_pointer`.

        Returns:
            The next model name, or ``None`` if rotation is disabled.
        """
        models = settings.CHAT_ROTATION_MODELS
        if not models:
            return None
        idx = self._chat_rotation_index
        self._chat_rotation_index = (idx + 1) % len(models)
        return models[idx]

    def bind_tools(self, tools: List) -> "LLMService":
        """Bind tools to the default LLM instance.

        Args:
            tools: List of tools to bind.

        Returns:
            Self for method chaining.
        """
        if self._llm:
            self._bound_tools = tools
            self._llm = self._llm.bind_tools(tools)
            logger.debug("tools_bound_to_llm", tool_count=len(tools))
        return self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(settings.MAX_LLM_CALL_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIError, OutputParserException)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _invoke_with_retry(self, llm: Any, messages: LanguageModelInput) -> Any:
        """Invoke an LLM runnable with automatic per-model retry logic.

        Args:
            llm: Any LangChain ``Runnable`` (plain model or structured-output chain).
            messages: Messages to send.

        Returns:
            The runnable's response (``BaseMessage`` or a ``BaseModel`` instance).

        Raises:
            OpenAIError: Propagated after all retry attempts are exhausted.
            OutputParserException: Propagated after retries (M-7 fix:
                structured-output parse failures now trigger retries
                because the LLM may produce valid output on the next
                attempt with a different sampling).
        """
        try:
            response = await llm.ainvoke(messages)
            logger.debug("llm_call_successful")
            return response
        except (RateLimitError, APITimeoutError, APIError) as e:
            logger.warning(
                "llm_call_failed_retrying",
                error_type=type(e).__name__,
                error=str(e),
                exc_info=True,
            )
            raise
        except OutputParserException as e:
            # M-7 fix: retry on structured-output parse failures. The LLM
            # may return valid JSON/schema on the next attempt.
            logger.warning(
                "llm_structured_output_parse_failed_retrying",
                error_type=type(e).__name__,
                error=str(e)[:200],
                exc_info=True,
            )
            raise
        except OpenAIError as e:
            logger.error(
                "llm_call_failed",
                error_type=type(e).__name__,
                error=str(e),
            )
            raise

    def _build_target(
        self,
        idx: int,
        response_format: Optional[Type[BaseModel]],
        model_kwargs: dict,
        bind_tools: bool = True,
        tools: Optional[List] = None,
    ) -> Any:
        """Build a runnable for a given registry index.

        For the default path (no kwargs/format override) this also binds
        ``self._bound_tools`` so tool calling works. The returned runnable
        is a fresh object — it does **not** mutate ``self._llm``.

        Args:
            idx: Index into ``LLMRegistry.LLMS``.
            response_format: Optional Pydantic schema for structured output.
            model_kwargs: Extra kwargs for the model constructor.
            bind_tools: When ``True`` (default), bind tools to the model.
                Pass ``False`` for calls that don't need tools (e.g. intent
                classification, debate) — especially when the target model
                may not support tool calling.
            tools: Explicit tool list to bind instead of ``self._bound_tools``.
                Used by agent nodes that need an agent-scoped tool subset
                (coder → file tools, tester → test tools).

        Returns:
            A LangChain ``Runnable`` ready to invoke.
        """
        base = LLMRegistry.get(LLMRegistry.LLMS[idx]["name"], **model_kwargs)
        if response_format:
            # Choose the structured-output method per model. function_calling is
            # the most reliable parser (the model returns tool_calls that LangChain
            # validates strictly against the Pydantic schema). However, some
            # compatible endpoints (deepseek via DashScope) HANG under
            # function_calling, so for those we fall back to json_mode — which
            # returns raw JSON our call_llm_structured recovery path can parse.
            model_name = LLMRegistry.LLMS[idx]["name"]
            if "deepseek" in model_name.lower() or "kimi" in model_name.lower():
                # deepseek (via DashScope) HANGS under function_calling, and kimi-k2
                # emits unstable tool_calls under its default thinking mode — both
                # surface as a None parse and crash the agent. json_mode returns raw
                # JSON that call_llm_structured's recovery path can parse reliably.
                try:
                    return base.with_structured_output(response_format, method="json_mode")
                except (TypeError, ValueError):
                    return base.with_structured_output(response_format)
            try:
                return base.with_structured_output(response_format, method="function_calling")
            except (TypeError, ValueError):
                pass
            try:
                # Fallback for models without native function calling support.
                return base.with_structured_output(response_format, method="json_mode")
            except (TypeError, ValueError):
                # Last resort: runnables whose with_structured_output() accepts no
                # `method` kwarg at all (older LangChain versions, test doubles).
                return base.with_structured_output(response_format)
        if bind_tools:
            selected_tools = tools if tools is not None else self._bound_tools
            if selected_tools:
                return base.bind_tools(selected_tools)
        return base

    def _advance_healthy_pointer(self, success_idx: int) -> None:
        """Promote the successful model index to the healthy pointer.

        Only called on the **success** path, never during fallback, so
        concurrent callers never race on this write. In asyncio's
        single-threaded model a simple assignment is safe.

        Args:
            success_idx: Registry index of the model that succeeded.
        """
        if success_idx != self._healthy_index:
            self._healthy_index = success_idx
            new_name = LLMRegistry.LLMS[success_idx]["name"]
            logger.info("healthy_pointer_advanced", new_model=new_name, new_index=success_idx)

    async def _call_with_fallback(
        self,
        messages: LanguageModelInput,
        model_name: Optional[str],
        response_format: Optional[Type[BaseModel]],
        model_kwargs: dict,
        bind_tools: bool = True,
        tools: Optional[List] = None,
    ) -> Union[BaseMessage, BaseModel]:
        """Build per-call strategy and delegate to the shared fallback loop.

        Both default and override paths share the same loop — the only
        difference is how ``get_target`` and ``start`` are resolved.
        All fallback state is **local** (per-call cursor), so concurrent
        requests never interfere.

        When ``model_name`` is explicitly specified (override call, e.g.
        session_naming → local-qwen-0.5b), the healthy pointer is NOT
        advanced — otherwise a lightweight-model success would hijack the
        default chat path's starting point.
        """

        def get_target(idx: int) -> Any:
            return self._build_target(idx, response_format, model_kwargs, bind_tools, tools)

        is_model_override = bool(model_name)
        requested_model = model_name or settings.DEFAULT_LLM_MODEL
        model_policy.validate(requested_model, get_llm_execution_context().purpose)
        if model_name or response_format or model_kwargs:
            all_names = LLMRegistry.get_all_names()
            if model_name and model_name not in all_names:
                logger.error("requested_model_not_found", model_name=model_name)
                raise ValueError(
                    f"model '{model_name}' not found in registry. available models: {', '.join(all_names)}"
                )
            start = all_names.index(model_name) if model_name else self._healthy_index
        else:
            start = self._healthy_index

        return await self._fallback_loop(
            messages,
            start,
            get_target,
            requested_model=requested_model,
            advance_health=not is_model_override,
        )

    async def _fallback_loop(
        self,
        messages: LanguageModelInput,
        start: int,
        get_target: Callable[[int], Any],
        requested_model: str,
        advance_health: bool = True,
    ) -> Any:
        """Per-call circular fallback — try each model in turn until success.

        Args:
            messages: Messages to send.
            start: Registry index to begin from (snapshot of healthy pointer).
            get_target: Returns the ``Runnable`` to invoke for a given index.
            requested_model: Model selected before fallback attempts begin.
            advance_health: When True, advance ``self._healthy_index`` on
                success. Should be False for override calls (explicit
                ``model_name``) so a lightweight-model success doesn't
                hijack the default chat path's starting point.

        Returns:
            The first successful response.

        Raises:
            RuntimeError: When all models have been exhausted.
        """
        total = len(LLMRegistry.LLMS)
        current = start
        models_tried = 0
        last_error: Optional[Exception] = None
        execution_context = get_llm_execution_context()

        for models_tried in range(1, total + 1):
            current_name = LLMRegistry.LLMS[current]["name"]
            if not model_policy.is_allowed(current_name, execution_context.purpose):
                logger.warning(
                    "llm_fallback_model_blocked_by_policy",
                    model=current_name,
                    purpose=execution_context.purpose,
                )
                current = (current + 1) % total
                continue
            identity = model_policy.get_identity(current_name)
            attempt_start = time.perf_counter()
            try:
                result = await self._invoke_with_retry(get_target(current), messages)
                await llm_execution_service.record(
                    execution_id=execution_context.execution_id,
                    user_id=execution_context.user_id,
                    agent_name=execution_context.agent_name,
                    purpose=execution_context.purpose,
                    requested_model=requested_model,
                    effective_model=current_name,
                    actual_model=identity.actual_model,
                    deployment=identity.deployment,
                    attempt=models_tried,
                    outcome="succeeded",
                    duration_ms=round((time.perf_counter() - attempt_start) * 1000),
                )
                # Advance the healthy pointer (success-only, no race).
                # Skipped for override calls to keep the default path stable.
                if advance_health:
                    self._advance_healthy_pointer(current)
                return result
            except OpenAIError as e:
                last_error = e
                await llm_execution_service.record(
                    execution_id=execution_context.execution_id,
                    user_id=execution_context.user_id,
                    agent_name=execution_context.agent_name,
                    purpose=execution_context.purpose,
                    requested_model=requested_model,
                    effective_model=current_name,
                    actual_model=identity.actual_model,
                    deployment=identity.deployment,
                    attempt=models_tried,
                    outcome="failed",
                    duration_ms=round((time.perf_counter() - attempt_start) * 1000),
                    error_type=type(e).__name__,
                )
                logger.error(
                    "llm_call_failed_after_retries",
                    model=current_name,
                    models_tried=models_tried,
                    total_models=total,
                    error=str(e),
                )
                if models_tried >= total:
                    logger.error(
                        "all_models_failed", models_tried=models_tried, starting_model=LLMRegistry.LLMS[start]["name"]
                    )
                    break
                current = (current + 1) % total

        raise RuntimeError(
            f"failed to get response from llm after trying {models_tried} models. last error: {str(last_error)}"
        )


llm_service = LLMService()
