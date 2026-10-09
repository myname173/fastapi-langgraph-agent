"""Shared context and helpers for SDLC agent nodes.

Provides the :class:`AgentContext` dataclass that bundles the services each
node needs, plus a :func:`build_default_context` factory that wires up the
module-level singletons (preserving the zero-arg ``SupervisorGraph()``
constructor contract).

Also provides two shared helpers that eliminate duplicated boilerplate
across the 5 agent node files:

- :func:`call_llm_structured` — wraps the metrics + tracing + LLM call
  pattern shared by planner/supervisor/reviewer nodes.
- :func:`build_error_command` — builds the standardized error Command
  returned by every node's ``except`` block.
"""

import dataclasses
import inspect
import json
import time
from typing import Any, Dict, List, Optional, Union

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import Command

from app.core.config import settings
from app.core.langgraph.tools import default_registry
from app.core.langgraph.tools.registry import ToolRegistry
from app.core.logging import logger
from app.core.metrics import sdlc_agent_errors_total, sdlc_agent_execution_seconds
from app.services.code_index_service import code_index_service
from app.services.knowledge_base_service import knowledge_base_service
from app.services.llm import llm_service
from app.services.llm.usage_tracker import set_llm_context
from app.services.llm.execution_context import set_llm_execution_context
from app.services.smart_search_service import smart_search_service
from app.utils import dump_sdlc_messages


@dataclasses.dataclass(frozen=True)
class AgentContext:
    """Immutable bundle of services injected into agent node factories.

    Attributes:
        llm_service: LLM caller with retry/fallback logic.
        registry: Central tool registry (replaces per-graph ``tools_by_name``
            dicts).
        code_index_service: Semantic code index for workspace search.
        smart_search_service: Hybrid search combining vector + knowledge graph.
        knowledge_base_service: RAG search over user-uploaded documents.
        model_router: Dynamic model router for cost-optimized model selection
            per agent role.
    """

    llm_service: Any  # LLMService — typed as Any to avoid circular import
    registry: ToolRegistry
    code_index_service: Any  # CodeIndexService
    smart_search_service: Any  # SmartSearchService
    knowledge_base_service: Any  # KnowledgeBaseService
    model_router: Any  # ModelRouter — typed as Any to avoid circular import


def build_default_context() -> AgentContext:
    """Build an :class:`AgentContext` from module-level singletons.

    This preserves the zero-arg ``SupervisorGraph()`` / ``LangGraphAgent()``
    constructor contract: the API layer instantiates these at import time
    without passing dependencies.
    """
    from app.services.llm.router import model_router

    return AgentContext(
        llm_service=llm_service,
        registry=default_registry,
        code_index_service=code_index_service,
        smart_search_service=smart_search_service,
        knowledge_base_service=knowledge_base_service,
        model_router=model_router,
    )


def get_node_metadata(
    agent_name: str,
    step: str,
    task_type: str = "general",
    token_usage: Optional[int] = None,
    success: Optional[bool] = None,
    human_intervention: Optional[bool] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Build enhanced metadata dict for Langfuse tracing.

    Shared by all agent nodes — extracted from the former
    ``SupervisorGraph._get_node_metadata`` method.

    Args:
        agent_name: Name of the agent (supervisor, planner, coder, ...).
        step: Current step in the workflow (routing, planning, coding, ...).
        task_type: Type of task (new_feature, bug_fix, general, etc.).
        token_usage: Token usage for this call.
        success: Whether the operation succeeded.
        human_intervention: Whether human intervention was required.
        error: Error message if any.

    Returns:
        Enhanced metadata dictionary for tracing.
    """
    metadata: Dict[str, Any] = {
        "agent_name": agent_name,
        "step": step,
        "task_type": task_type,
        "environment": settings.ENVIRONMENT.value,
    }

    if token_usage is not None:
        metadata["token_usage"] = token_usage
    if success is not None:
        metadata["success"] = success
    if human_intervention is not None:
        metadata["human_intervention"] = human_intervention
    if error is not None:
        metadata["error"] = error

    return metadata


def record_handoff(
    from_agent: str,
    to_agent: str,
    state_delta_keys: list[str],
    latency_ms: float,
    error: Optional[str] = None,
) -> None:
    """Record a structured handoff event for multi-agent tracing.

    Called automatically by the node wrapper in
    :func:`app.core.langgraph.workflows.supervisor.SupervisorGraph.create_graph`
    when ``settings.ENABLE_HANDOFF_TRACING`` is true.

    Emits a structured log event ``sdlc_handoff`` with:
    - ``from_agent`` / ``to_agent``: the transition pair
    - ``state_delta_keys``: field names in the ``Command.update`` payload
      (values are NOT logged to avoid leaking sensitive content)
    - ``latency_ms``: wall-clock time spent inside the node
    - ``error``: exception message if the node raised

    This data can be correlated in any log aggregator (Loki, ELK,
    CloudWatch) to visualise agent handoffs and detect "handoff
    failures" — where Agent A passes incomplete context to Agent B.

    Args:
        from_agent: Name of the node that produced the Command.
        to_agent: ``Command.goto`` target (node name or ``"END"``).
        state_delta_keys: Keys of the ``Command.update`` dict. Only key
            names are recorded, never values.
        latency_ms: Node execution time in milliseconds.
        error: Exception message if the node failed.
    """
    logger.info(
        "sdlc_handoff",
        from_agent=from_agent,
        to_agent=to_agent,
        state_delta_keys=state_delta_keys,
        latency_ms=round(latency_ms, 2),
        error=error,
    )


def wrap_node_with_handoff(
    node_name: str,
    node_func: Any,
) -> Any:
    """Wrap an SDLC agent node to record handoff events.

    The wrapper preserves the LangGraph node contract
    ``async (state, config) -> Command`` while automatically recording
    a :func:`record_handoff` event when the node returns.

    When ``settings.ENABLE_HANDOFF_TRACING`` is false, the wrapper is a
    no-op pass-through (zero overhead).

    Args:
        node_name: Name of the node (e.g. ``"supervisor"``, ``"coder"``).
        node_func: The original async node callable.

    Returns:
        A wrapped callable with the same signature.
    """
    # Some nodes (planner) take only (state); others take (state, config).
    _accepts_config = len(inspect.signature(node_func).parameters) >= 2

    async def _wrapped(state: Any, config: RunnableConfig | None = None) -> Any:
        if config is None:
            config = {}

        async def _invoke() -> Any:
            configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
            metadata = config.get("metadata", {}) if isinstance(config, dict) else {}
            user_id = metadata.get("user_id")
            with set_llm_execution_context(
                execution_id=configurable.get("thread_id"),
                user_id=int(user_id) if user_id else None,
                agent_name=node_name,
                purpose=node_name,
            ):
                if _accepts_config:
                    return await node_func(state, config)
                return await node_func(state)

        if not settings.ENABLE_HANDOFF_TRACING:
            return await _invoke()

        start = time.perf_counter()
        try:
            result = await _invoke()
            latency_ms = (time.perf_counter() - start) * 1000

            # Extract handoff metadata from the returned Command.
            # LangGraph's Command has ``goto`` and ``update`` attributes.
            to_agent = getattr(result, "goto", "unknown")
            update = getattr(result, "update", None) or {}
            state_delta_keys = list(update.keys()) if isinstance(update, dict) else []

            # Command.goto can be a list (multi-route) — normalise to str.
            if isinstance(to_agent, list):
                to_agent = ",".join(str(g) for g in to_agent)
            else:
                to_agent = str(to_agent)

            record_handoff(
                from_agent=node_name,
                to_agent=to_agent,
                state_delta_keys=state_delta_keys,
                latency_ms=latency_ms,
            )
            return result

        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            record_handoff(
                from_agent=node_name,
                to_agent="__error__",
                state_delta_keys=[],
                latency_ms=latency_ms,
                error=str(e),
            )
            raise

    return _wrapped


# ---------------------------------------------------------------------------
# Shared node helpers — eliminate duplicated boilerplate across agent nodes.
# ---------------------------------------------------------------------------


def _extract_json_blob(text: str) -> str:
    """Pull the first JSON object/array out of a free-form string.

    Handles three common shapes from compatible-mode providers:
    - fenced code blocks (```json ... ```)
    - a bare JSON object/array
    - prose wrapping a JSON object/array
    """
    if not text:
        return ""
    stripped = text.strip()
    # fenced code block
    if stripped.startswith("```"):
        # drop opening fence (``` or ```json) and closing fence
        body = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        stripped = body.strip()
    try:
        json.loads(stripped)
        return stripped
    except json.JSONDecodeError:
        pass
    # find outermost braces / brackets
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = stripped.find(open_ch)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(stripped)):
            if stripped[i] == open_ch:
                depth += 1
            elif stripped[i] == close_ch:
                depth -= 1
                if depth == 0:
                    return stripped[start : i + 1]
    return ""


def _recover_structured_output(
    message: BaseMessage,
    response_format: type,
    agent_name: str,
    step: str,
) -> Any:
    """Recover a parsed schema instance when ``with_structured_output`` fails.

    Compatible-mode providers sometimes return the raw ``AIMessage`` (with
    ``additional_kwargs["parsed"]`` either missing or ``None``) instead of a
    validated schema instance. This extracts the JSON from ``content`` or
    ``additional_kwargs`` and validates it against ``response_format``.

    Raises:
        OutputParserException: If no valid JSON matching the schema can be
        recovered — preserves the existing retry-on-parse-failure behaviour.
    """
    from langchain_core.exceptions import OutputParserException

    raw_candidates: list = []
    parsed_kw = message.additional_kwargs.get("parsed")
    if parsed_kw:
        raw_candidates.append(parsed_kw if isinstance(parsed_kw, str) else json.dumps(parsed_kw))
    if isinstance(message.content, str):
        raw_candidates.append(message.content)
    elif isinstance(message.content, list):
        # Some providers wrap content as a list of text parts
        for part in message.content:
            if isinstance(part, dict) and part.get("type") == "text":
                raw_candidates.append(part.get("text", ""))

    last_error: Optional[Exception] = None
    for raw in raw_candidates:
        blob = _extract_json_blob(raw)
        if not blob:
            continue
        try:
            return response_format.model_validate_json(blob)
        except Exception as e:  # noqa: BLE001 - try next candidate
            last_error = e
            logger.warning(
                "sdlc_structured_recovery_failed",
                agent_name=agent_name,
                step=step,
                error=str(e)[:200],
            )
    logger.error(
        "sdlc_structured_recovery_exhausted",
        agent_name=agent_name,
        step=step,
        content_preview=str(message.content)[:200],
    )
    raise OutputParserException(f"could not recover structured output for {response_format.__name__}") from last_error


def _recover_from_parse_error(
    exc: "OutputParserException",
    response_format: type,
    agent_name: str,
    step: str,
) -> Any:
    """Best-effort recovery when ``with_structured_output`` raises.

    Compatible-mode models sometimes emit a JSON *array* where the Pydantic
    schema expects an *object* (e.g. returning the list of code changes
    directly instead of a ``CoderOutput`` wrapper). LangChain surfaces this as
    ``OutputParserException("Input should be an object")`` and discards the
    payload. We recover by:

    1. Pulling the raw model output from ``exc.llm_output`` (LangChain keeps it).
    2. If it parses as a JSON array, wrap it into the first ``List[...]``-typed
       field of ``response_format`` (e.g. ``code_changes``), then validate.
    3. Otherwise try a direct object parse via ``model_validate_json``.

    Returns the validated instance, or ``None`` if recovery is impossible (the
    caller then re-raises, preserving the existing retry-on-failure behaviour).
    """
    raw = getattr(exc, "llm_output", None)
    if isinstance(raw, BaseMessage):
        raw = raw.content
    if not raw:
        return None
    if not isinstance(raw, str):
        raw = json.dumps(raw)
    blob = _extract_json_blob(raw)
    if not blob:
        return None

    # Case A: model returned an array but schema wants an object.
    if blob.lstrip().startswith("["):
        try:
            arr = json.loads(blob)
        except json.JSONDecodeError:
            return None
        # Find the first List[...]-typed field (including Optional[List[...]])
        # and wrap the array there. Compatible-mode providers frequently emit
        # the bare list of items instead of the {field: [...]} wrapper object.

        for name, field in response_format.model_fields.items():
            annotation = field.annotation
            # Unwrap Optional[...] / Union[..., None] to the inner type.
            origin = getattr(annotation, "__origin__", None)
            args = getattr(annotation, "__args__", None) or ()
            inner = annotation
            if origin is Union:
                # Optional[X] == Union[X, None]; pick the non-None member.
                non_none = [a for a in args if a is not type(None)]
                if non_none:
                    inner = non_none[0]
                    origin = getattr(inner, "__origin__", None)
            if origin in (list, List):
                try:
                    wrapped = response_format.model_validate({name: arr})
                    logger.warning(
                        "sdlc_structured_recovered_array_as_object",
                        agent_name=agent_name,
                        step=step,
                        field=name,
                    )
                    return wrapped
                except Exception:  # noqa: BLE001 - fall through to next field
                    continue
        # Fallback: try validating the array directly (rare).
        try:
            return response_format.model_validate(arr)
        except Exception:  # noqa: BLE001
            return None

    # Case B: model returned an object-shaped JSON; validate directly.
    try:
        return response_format.model_validate_json(blob)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "sdlc_structured_recovery_failed",
            agent_name=agent_name,
            step=step,
            error=str(e)[:200],
        )
        return None


async def call_llm_structured(
    ctx: AgentContext,
    agent_name: str,
    step: str,
    messages: list,
    response_format: type,
) -> Any:
    """Call LLM with structured output, wrapped with metrics and tracing.

    Consolidates the repeated pattern shared by planner/supervisor/reviewer
    nodes:

    1. Record execution time via ``sdlc_agent_execution_seconds``.
    2. Set LLM context for cost tracking via ``set_llm_context``.
    3. Call ``llm_service.call`` with model_router + response_format +
       metadata.

    Args:
        ctx: Agent context with ``llm_service`` and ``model_router``.
        agent_name: Name of the agent (``"planner"``, ``"supervisor"``,
            ``"reviewer"``).
        step: Current workflow step — also used as ``task_type`` for cost
            tracking (``"planning"``, ``"routing"``, ``"reviewing"``).
        messages: Prepared messages list (output of
            ``prepare_sdlc_messages``).
        response_format: Pydantic model class for structured output.

    Returns:
        Parsed structured response from the LLM.
    """
    # L-1 fix: record the actual success/fail status in the metric label
    # instead of hardcoding "success". The .time() context manager fixes
    # the label at entry, so we time the call first, then observe with
    # the correct status afterwards.
    _start = time.perf_counter()
    _success = False
    try:
        with set_llm_context(agent_name, step):
            result = await ctx.llm_service.call(
                dump_sdlc_messages(messages),
                model_name=ctx.model_router.get_model_for_call(agent_name),
                response_format=response_format,
                metadata=get_node_metadata(agent_name, step, success=True),
            )
        # S-1 fix: some compatible-mode providers (e.g. DashScope qwen via the
        # OpenAI-compatible endpoint) return the raw AIMessage instead of a
        # parsed schema instance when with_structured_output fails to surface the
        # JSON. Recover by extracting JSON from content / additional_kwargs and
        # validating it against response_format before giving up.
        if isinstance(result, BaseMessage):
            result = _recover_structured_output(result, response_format, agent_name, step)
        _success = True
        return result
    except OutputParserException as e:
        # S-6 fix: a frequent compatible-mode failure mode is the model returning
        # a JSON *array* (e.g. the list of code changes) instead of the expected
        # *object* wrapper (e.g. CoderOutput with a `code_changes` field).
        # with_structured_output raises OutputParserException("Input should be an
        # object") and discards the payload. Recover by re-parsing the raw LLM
        # output: if it is an array, wrap it into the first List-typed field of
        # the schema; otherwise try a direct object parse.
        recovered = _recover_from_parse_error(e, response_format, agent_name, step)
        if recovered is not None:
            _success = True
            return recovered
        raise
    finally:
        _duration = time.perf_counter() - _start
        sdlc_agent_execution_seconds.labels(
            agent_name=agent_name,
            step=step,
            status="success" if _success else "fail",
        ).observe(_duration)


def build_error_command(
    agent_name: str,
    step: str,
    status: str,
    error: Exception,
) -> Command:
    """Build a standardized error Command for agent node failure paths.

    All SDLC agent nodes share the same error-handling pattern: increment
    the error counter, log the failure, and return a Command that routes
    back to the supervisor with the error details. This helper eliminates
    the duplication across 5 node files.

    The log event name is unified to ``sdlc_agent_parsing_failed`` with
    ``agent_name`` as a kwarg (instead of per-agent event names like
    ``sdlc_planner_parsing_failed``), conforming to structlog best
    practices.

    Args:
        agent_name: Name of the agent (``"planner"``, ``"coder"``, etc.).
        step: Current workflow step (``"planning"``, ``"coding"``, etc.).
        status: State status to set (``"planning"``, ``"coding"``, etc.).
        error: The exception that caused the failure.

    Returns:
        Command routing to ``"supervisor"`` with error details.
    """
    sdlc_agent_errors_total.labels(agent_name=agent_name, step=step).inc()
    logger.error(
        "sdlc_agent_parsing_failed",
        agent_name=agent_name,
        error=str(error),
    )
    title = agent_name.capitalize()
    return Command(
        update={
            "messages": [{"role": "assistant", "content": f"{title} error: {str(error)}"}],
            "errors": [{"agent_name": agent_name, "content": str(error), "severity": "high"}],
            "status": status,
        },
        goto="supervisor",
    )
