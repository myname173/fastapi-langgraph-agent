"""This file contains the LangGraph Agent/workflow and interactions with the LLM."""

import asyncio
import re
import time
from typing import (
    AsyncGenerator,
    Optional,
    Union,
    cast,
)

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    convert_to_openai_messages,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import (
    END,
    StateGraph,
)
from langchain_core.runnables.config import RunnableConfig
from langgraph.graph.state import (
    Command,
    CompiledStateGraph,
)
from langgraph.types import (
    RetryPolicy,
    StateSnapshot,
)
from psycopg import sql

from app.core.config import (
    Environment,
    settings,
)
from app.core.db.pool import (
    PostgresConnPool,
    init_pool,
)
from app.core.langgraph.tools import default_registry
from app.core.logging import logger
from app.core.metrics import llm_inference_duration_seconds
from app.core.observability import langfuse_callback_handler
from app.core.chat_intent import (
    ChatIntent,
    IntentResult,
    classify_chat_intent,
)
from app.core.prompts import load_system_prompt
from app.core.symbol_extract import (
    DISCLAIMER_TEXT,
    extract_a_share_symbols,
    needs_disclaimer,
)
from app.core.task_manager import task_manager
from app.services import finance_memory
from app.services.risk_profile_service import risk_profile_service
from app.schemas import (
    GraphState,
    Message,
)
from app.services.image_service import image_service
from app.services.llm import llm_service
from app.services.llm.execution_context import set_llm_execution_context
from app.services.memory import memory_service
from app.services.quota_service import quota_service
from app.schemas.chat import ChatImage
from app.utils import (
    dump_messages,
    extract_text_content,
    prepare_messages,
    process_llm_response,
)


def _extract_last_user_text(messages: list) -> str:
    """Return the text of the most recent HumanMessage in the history.

    Args:
        messages: The chat message history (LangChain message objects).

    Returns:
        The latest user text, or an empty string if none is found.
    """
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def _build_local_casual_messages(user_text: str) -> list:
    """Build a minimal message set for the local 1.5B casual path.

    The local model has a tiny 1536-token context, so we must NOT forward the
    full conversation history or the long system prompt. We send a short,
    friendly system prompt plus only the latest user message.

    Args:
        user_text: The latest user message text.

    Returns:
        A list of ``Message`` objects ready for ``dump_messages``.
    """
    return [
        Message(
            role="system",
            content="你是友好、简洁、口语化的中文对话助手，回答尽量简短自然。",
        ),
        Message(role="user", content=user_text[:400]),
    ]


def _triggered_realtime_tools(messages: list) -> bool:
    """Detect whether the most recent model decision used real-time tools.

    Used to suppress semantic caching for answers derived from live market
    data (so we never serve a stale quote from cache).

    Args:
        messages: The chat message history (LangChain message objects).

    Returns:
        True if the latest AIMessage issued a tool call whose name is in
        ``settings.REALTIME_TOOL_NAMES``.
    """
    if not settings.REALTIME_TOOL_NAMES:
        return False
    realtime = set(settings.REALTIME_TOOL_NAMES)
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                return any(isinstance(tc, dict) and tc.get("name") in realtime for tc in tool_calls)
            # A pure-text AI turn with no tool calls -> no real-time data.
            return False
        if isinstance(msg, HumanMessage):
            # Fresh user turn with no preceding tool usage.
            return False
    return False


def _extract_symbol_from_tools(messages: list) -> tuple[str, str]:
    """Find the first real-time tool call's ``symbol`` argument.

    Args:
        messages: The chat message history (LangChain message objects).

    Returns:
        Tuple ``(symbol, name)``; either may be empty if not found.
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", []) or []:
                if not isinstance(tc, dict):
                    continue
                args = tc.get("args") or {}
                symbol = args.get("symbol")
                if symbol:
                    return str(symbol), str(args.get("name") or "")
    return "", ""


def _format_risk_profile(rp) -> str:
    """Render a risk profile as bullet text for the system prompt.

    Args:
        rp: A ``RiskProfile`` instance.

    Returns:
        Multi-line summary string.
    """
    return (
        "\n[用户风险画像]\n"
        f"- 风险偏好：{rp.risk_tolerance.value}\n"
        f"- 投资目标：{rp.investment_goals or '未设置'}\n"
        f"- 投资周期：{rp.horizon or '未设置'}\n"
        f"- 月均可投资：{rp.monthly_invest}\n"
        f"- 经验：{rp.experience or '未设置'}"
    )


# Maps internal tool names to a human-readable label shown as a progress chip
# in the chat UI while the agent is calling the tool (streamed via SSE).
# Keys MUST match the registered @tool function names (see tools/*_tools.py).
TOOL_LABELS: dict[str, str] = {
    "fetch_realtime_quote": "查询实时行情",
    "fetch_fundamentals": "读取基本面",
    "fetch_kline": "计算技术指标",
    "fetch_news_sentiment": "检索新闻舆情",
    "generate_chart": "生成图表",
    "research_debate": "多空研究辩论",
    "duckduckgo_search": "联网搜索",
    "search_knowledge_base": "检索知识库",
    "ask_human": "等待人工确认",
}


class _ToolEventCollector(BaseCallbackHandler):
    """Forward tool start/end events to an ``asyncio.Queue``.

    ``get_stream_response`` drains this queue concurrently with the token
    stream so the frontend can display "正在查询实时行情…" progress chips
    without blocking text delivery.
    """

    def __init__(self, queue: "asyncio.Queue") -> None:
        super().__init__()
        self._queue = queue

    async def on_tool_start(
        self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs
    ):
        name = (serialized or {}).get("name") if isinstance(serialized, dict) else None
        label = TOOL_LABELS.get(name or "", name or "调用工具")
        await self._queue.put({"type": "tool", "label": label})

    async def on_tool_end(self, output, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        await self._queue.put({"type": "tool", "label": None})


# Matches the Markdown image line emitted by the ``generate_chart`` tool, e.g.
# ``![白酒对比](/charts/chart_ab12cd34.svg)``.
_CHART_MD_RE = re.compile(r"!\[[^\]]*\]\(/charts/[^\s)]+\)")


def _latest_chart_markdown(messages: list) -> Optional[str]:
    """Return the newest chart image line produced by ``generate_chart``.

    The chart tool returns a Markdown image (``![title](/charts/x.svg)``) so
    the frontend can render it, but the LLM frequently *paraphrases* the tool
    output and drops that line from its final answer — the chart then never
    shows up. Scanning the turn's tool messages lets the caller re-attach the
    image line verbatim, guaranteeing the chart renders.
    """
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            content = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        if not isinstance(content, str):
            continue
        match = _CHART_MD_RE.search(content)
        if match:
            return match.group(0)
    return None


class LangGraphAgent:
    """Manages the LangGraph Agent/workflow and interactions with the LLM.

    This class handles the creation and management of the LangGraph workflow,
    including LLM interactions, database connections, and response processing.
    """

    def __init__(self):
        """Initialize the LangGraph Agent with necessary components."""
        # Use the LLM service with tools bound
        self.llm_service = llm_service
        self.llm_service.bind_tools(default_registry.get_for_agent("chat"))
        self._registry = default_registry
        self._graph: Optional[CompiledStateGraph] = None
        logger.info(
            "langgraph_agent_initialized",
            model=settings.DEFAULT_LLM_MODEL,
            environment=settings.ENVIRONMENT.value,
        )

    async def _get_connection_pool(self) -> Optional[PostgresConnPool]:
        """Get the shared PostgreSQL connection pool.

        Delegates to :func:`app.core.db.pool.init_pool` so the pool is
        shared with the business DB layer. The pool is created on first
        call (lazy init) and reused for the lifetime of the process.

        Returns:
            The shared ``AsyncConnectionPool``, or ``None`` when the
            pool fails to initialise in production (degraded mode).
        """
        return await init_pool()

    async def _chat(self, state: GraphState, config: RunnableConfig) -> Command:
        """Process the chat state and generate a response.

        Args:
            state (GraphState): The current state of the conversation.
            config (RunnableConfig): The runnable configuration for this invocation.

        Returns:
            Command: Command object with updated state and next node to execute.
        """
        # Get the current LLM instance for metrics
        current_llm = self.llm_service.get_llm()
        model_name = (
            current_llm.model_name
            if current_llm and hasattr(current_llm, "model_name")
            else settings.DEFAULT_LLM_MODEL
        )

        username = config.get("metadata", {}).get("username")
        user_id = config.get("metadata", {}).get("user_id")
        thread_id = config.get("configurable", {}).get("thread_id")

        # Inject the user's risk profile into the system prompt (P2), so the
        # advisor tailors guidance. Only when enabled and a profile exists;
        # failures are ignored so chat never breaks on a profile lookup.
        long_term_memory = state["long_term_memory"]
        if settings.ENABLE_RISK_PROFILE and user_id:
            try:
                _rp = await risk_profile_service.get(int(user_id))
                if _rp:
                    long_term_memory = (long_term_memory or "") + "\n" + _format_risk_profile(_rp)
            except Exception:
                pass

        SYSTEM_PROMPT = load_system_prompt(
            username=username,
            long_term_memory=long_term_memory,
            vision_context=state.get("vision_context", ""),
        )

        # Prepare messages with system prompt
        messages = prepare_messages(state["messages"], SYSTEM_PROMPT)

        # --- Chat model adaptation (per-turn routing) ---
        # All flags default off, so with nothing enabled this block is a no-op
        # and behaviour is identical to the original path.
        model_kwargs: dict = {}
        cacheable = True
        call_model_name = None  # None -> resolves to DEFAULT_LLM_MODEL
        is_local_casual = False
        last_user_text = _extract_last_user_text(state["messages"])
        # ── Local intent classification + tool/symbol extraction ────────
        # `classify_chat_intent` now returns IntentResult(intent, tool, symbol).
        # The tool/symbol fields are only populated when intent = TOOL_NEEDED
        # and the local 1.5B classifier parsed them successfully.
        #
        # Streaming path pre-computes intent OUTSIDE the graph (in
        # get_stream_response) and passes it via config metadata, so the
        # local 1.5B's output doesn't leak into the SSE stream.
        intent_result: IntentResult | None = config.get("metadata", {}).get("intent_result")
        if intent_result is None and settings.ENABLE_CHAT_MODEL_ROUTING:
            intent_result = await classify_chat_intent(last_user_text)
        if settings.ENABLE_CHAT_MODEL_ROUTING:
            intent = intent_result.intent
            if intent == ChatIntent.CASUAL and settings.ENABLE_LOCAL_CHAT_TIER:
                # Short chit-chat -> local 1.5B (free, low latency). Use the
                # minimal message set because the local model has a tiny ctx.
                call_model_name = settings.LOCAL_CHAT_MODEL
                model_kwargs = {"temperature": settings.CHAT_CASUAL_TEMPERATURE}
                messages = _build_local_casual_messages(last_user_text)
                is_local_casual = True
            else:
                # analytical / tool_needed -> cloud heavy model. Keep the
                # DEFAULT_LLM_MODEL (zero regression) but tune the budget:
                # analytical reports can be long (quotes + fundamentals + news).
                model_kwargs = {
                    "temperature": settings.CHAT_ANALYTICAL_TEMPERATURE,
                    "max_tokens": settings.CHAT_ANALYTICAL_MAX_TOKENS,
                }
                if settings.CHAT_ENABLED_REASONING:
                    model_kwargs["reasoning"] = True
            # Never cache answers derived from real-time market data, so a later
            # identical query does not return a stale quote.
            if _triggered_realtime_tools(state["messages"]):
                cacheable = False
                # Best-effort layered memory: remember symbols the user asks
                # about, so the advisor "knows the user" across sessions.
                if settings.ENABLE_FINANCIAL_MEMORY:
                    _symbol, _name = _extract_symbol_from_tools(state["messages"])
                    if _symbol and isinstance(user_id, str):
                        await finance_memory.remember_symbol_if_new(user_id, _symbol, _name)
        # --- end adaptation ---

        # Non-binding symbol hint: regex-extracted A-share codes are appended
        # as a system message so the cloud LLM can use them as tool-arg
        # reference. Independent of ENABLE_CHAT_MODEL_ROUTING — fires whenever
        # the cloud model is used (the default path when routing is off), so
        # ENABLE_SYMBOL_HINT works standalone. Skipped on the local CASUAL
        # path (tiny ctx, no tools bound, messages already trimmed). Zero LLM
        # cost, whitelist-filtered, explicitly framed as ignorable — never
        # forces a tool call. Empty match -> no hint, zero regression.
        if settings.ENABLE_SYMBOL_HINT and not is_local_casual:
            _hint_syms = extract_a_share_symbols(last_user_text)
            if _hint_syms:
                messages.append(
                    Message(
                        role="system",
                        content=(
                            "[标的提示] 用户消息中检测到代码："
                            + "、".join(_hint_syms)
                            + "。请作为工具参数参考，若与用户意图不符请忽略。"
                        ),
                    )
                )

        # Local tool + symbol hint: when intent classification is ON and the
        # local 1.5B produced a TOOL_NEEDED result with tool/symbol, inject
        # them as a structured system prompt so the cloud LLM can skip a
        # round of tool-selection inference.  Non-binding — the cloud agent
        # still decides which tool to call.  Independent of ENABLE_SYMBOL_HINT
        # (regex) — complementary: regex catches all symbols, local 1.5B adds
        # intent-aware tool prediction.
        if (
            not is_local_casual
            and intent_result is not None
            and intent_result.intent == ChatIntent.TOOL_NEEDED
            and intent_result.tool
        ):
            hint_parts = [f"工具: {intent_result.tool}"]
            if intent_result.symbol:
                hint_parts.append(f"代码: {intent_result.symbol}")
            hint_parts.append("若预判有误请根据实际消息重新选择工具和参数")
            messages.append(
                Message(
                    role="system",
                    content="[工具预判] " + "，".join(hint_parts),
                )
            )

        # Per-message model rotation (round-robin). Independent from the
        # healthy pointer: _call_with_fallback sets advance_health=False for
        # override calls, so rotation never corrupts the default fallback
        # chain. Skipped when call_model_name was already set by intent
        # routing (e.g. CASUAL → local) so that path keeps priority.
        if settings.CHAT_ROTATION_MODELS and not call_model_name:
            call_model_name = self.llm_service.next_chat_rotation_model()

        try:
            # Use LLM service with automatic retries and circular fallback.
            # Enable semantic cache for chat — semantically similar user
            # queries hit the cache without an LLM call (unless suppressed for
            # real-time market data above).
            purpose = "chat_casual" if is_local_casual else "chat"
            with set_llm_execution_context(
                execution_id=thread_id,
                user_id=int(user_id) if user_id else None,
                agent_name="chat",
                purpose=purpose,
            ):
                with llm_inference_duration_seconds.labels(model=call_model_name or model_name).time():
                    response_message = await self.llm_service.call(
                        dump_messages(messages),
                        model_name=call_model_name,
                        cacheable=cacheable,
                        cache_user_id=user_id,
                        **model_kwargs,
                    )

            # Process response to handle structured content blocks
            response_message = process_llm_response(response_message)

            logger.info(
                "llm_response_generated",
                session_id=thread_id,
                model=call_model_name or model_name,
                environment=settings.ENVIRONMENT.value,
            )

            # Determine next node based on whether there are tool calls
            if isinstance(response_message, AIMessage) and response_message.tool_calls:
                goto = "tool_call"
            else:
                goto = END

            return Command(update={"messages": [response_message]}, goto=goto)
        except Exception as e:
            logger.error(
                "llm_call_failed_all_models",
                session_id=thread_id,
                error=str(e),
                environment=settings.ENVIRONMENT.value,
            )
            raise Exception(f"failed to get llm response after trying all models: {str(e)}")

    # Define our tool node
    async def _tool_call(self, state: GraphState, config: RunnableConfig) -> Command:
        """Process tool calls from the last message.

        Args:
            state: The current agent state containing messages and tool calls.
            config: The runnable configuration, passed through to tools so
                they can access ``config["metadata"]["user_id"]`` etc.

        Returns:
            Command: Command object with updated messages and routing back to chat.
        """
        tool_calls = state["messages"][-1].tool_calls

        async def _execute_tool(tool_call: dict) -> ToolMessage:
            tool_result = await self._registry.get_by_name(tool_call["name"]).ainvoke(tool_call["args"], config=config)
            return ToolMessage(
                content=tool_result,
                name=tool_call["name"],
                tool_call_id=tool_call["id"],
            )

        # Execute tool calls concurrently when multiple are requested
        if len(tool_calls) == 1:
            outputs = [await _execute_tool(tool_calls[0])]
        else:
            outputs = list(await asyncio.gather(*[_execute_tool(tc) for tc in tool_calls]))

        return Command(update={"messages": outputs}, goto="chat")

    async def create_graph(self) -> Optional[CompiledStateGraph]:
        """Create and configure the LangGraph workflow.

        Returns:
            Optional[CompiledStateGraph]: The configured LangGraph instance or None if init fails
        """
        if self._graph is None:
            try:
                graph_builder = StateGraph(GraphState)
                graph_builder.add_node("chat", self._chat, destinations=("tool_call", END))
                graph_builder.add_node(
                    "tool_call",
                    self._tool_call,
                    destinations=("chat",),
                    retry_policy=RetryPolicy(max_attempts=3),
                )
                graph_builder.set_entry_point("chat")
                graph_builder.set_finish_point("chat")

                # Get connection pool (may be None in production if DB unavailable)
                connection_pool = await self._get_connection_pool()
                if connection_pool:
                    checkpointer = AsyncPostgresSaver(connection_pool)
                    await checkpointer.setup()
                else:
                    # In production, proceed without checkpointer if needed
                    checkpointer = None
                    if settings.ENVIRONMENT != Environment.PRODUCTION:
                        raise Exception("Connection pool initialization failed")

                self._graph = graph_builder.compile(
                    checkpointer=checkpointer, name=f"{settings.PROJECT_NAME} Agent ({settings.ENVIRONMENT.value})"
                )

                logger.info(
                    "graph_created",
                    graph_name=f"{settings.PROJECT_NAME} Agent",
                    environment=settings.ENVIRONMENT.value,
                    has_checkpointer=checkpointer is not None,
                )
            except Exception as e:
                logger.error("graph_creation_failed", error=str(e), environment=settings.ENVIRONMENT.value)
                # In production, we don't want to crash the app
                if settings.ENVIRONMENT == Environment.PRODUCTION:
                    logger.warning("continuing_without_graph")
                    return None
                raise e

        return self._graph

    async def _get_graph(self) -> CompiledStateGraph:
        """Return the compiled graph, creating it on first access.

        Raises:
            RuntimeError: When ``create_graph()`` swallowed an init failure
                (production-only path) and returned ``None``. Callers can
                rely on the return being non-``None``.
        """
        if self._graph is None:
            self._graph = await self.create_graph()
        if self._graph is None:
            raise RuntimeError("graph initialization failed")
        return self._graph

    async def _resolve_vision_context(
        self,
        images: Optional[list[ChatImage]],
        user_id: Optional[str],
        session_id: str,
    ) -> Optional[str]:
        """Resolve uploaded chat images into a text vision-context block.

        Routes each image through the vision model (Route B) and concatenates
        describe-only summaries for injection into the system prompt. Returns
        None when multimodal is disabled, no images are attached, or every
        analysis fails — so the caller simply proceeds with plain-text chat.

        Args:
            images: Optional chat images from the request.
            user_id: User id, used for daily quota accounting.
            session_id: Chat session id, used for log correlation.

        Returns:
            Optional[str]: Concatenated vision context, or None.
        """
        if not settings.ENABLE_MULTIMODAL or not images:
            return None
        # Adapt the LangGraph-layer user_id (str from config metadata) to the
        # service-layer contract (int|None). quota_service and image_service
        # use int for DB joins; str only flows inside the LangGraph config.
        uid: Optional[int] = int(user_id) if user_id else None
        blocks: list[str] = []
        for img in images:
            if not quota_service.check_vision_quota(uid):
                logger.warning("vision_quota_exceeded", session_id=session_id, user_id=user_id)
                break
            try:
                # Use an ephemeral per-request namespace: chat uploads are
                # transient and must not accumulate under the session workspace
                # or exhaust MAX_IMAGES_PER_TASK over a long conversation.
                analysis = await image_service.analyze_investment_image(
                    image_input=img.data,
                    user_id=uid,
                    temp=True,
                )
            except Exception as e:
                logger.warning("vision_context_single_failed", session_id=session_id, error=str(e))
                continue
            if analysis is None:
                continue
            line = f"- {analysis.description}"
            if analysis.key_elements:
                line += f"\n  关键要素: {', '.join(analysis.key_elements)}"
            blocks.append(line)
            quota_service.increment_vision_usage(uid)
        if not blocks:
            return None
        return "以下是用户本次上传图片的视觉解读（由视觉模型生成，仅供参考，不构成投资建议）：\n" + "\n".join(blocks)

    async def get_response(
        self,
        messages: list[Message],
        session_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        images: Optional[list[ChatImage]] = None,
    ) -> list[Message]:
        """Get a response from the LLM.

        Args:
            messages (list[Message]): The messages to send to the LLM.
            session_id (str): The session ID for the conversation.
            user_id (Optional[str]): The user ID for the conversation.
            username (Optional[str]): The display name of the user.
            images (Optional[list[ChatImage]]): Uploaded images for vision context.

        Returns:
            list[Message]: The response from the LLM.
        """
        graph = await self._get_graph()
        callbacks: list[BaseCallbackHandler] = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
        config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            "callbacks": callbacks,
            "metadata": {
                "user_id": user_id,
                "username": username,
                "session_id": session_id,
                "environment": settings.ENVIRONMENT.value,
                "debug": settings.DEBUG,
            },
        }

        try:
            t0 = time.perf_counter()
            # Run state check and memory search concurrently to save 200-500ms
            state, relevant_memory = await asyncio.gather(
                graph.aget_state(config),
                memory_service.search(user_id, messages[-1].content),
            )
            memory_latency_ms = round((time.perf_counter() - t0) * 1000)

            vision_context = await self._resolve_vision_context(images, user_id, session_id)

            if state.next:
                logger.info("resuming_interrupted_graph", session_id=session_id, next_nodes=state.next)
                response = await graph.ainvoke(
                    Command(resume=messages[-1].content),
                    config=config,
                )
            else:
                relevant_memory = relevant_memory or "No relevant memory found."
                response = await graph.ainvoke(
                    input={
                        "messages": dump_messages(messages),
                        "long_term_memory": relevant_memory,
                        "vision_context": vision_context or "",
                    },
                    config=config,
                )

            # Check if the graph was interrupted during this invocation
            state = await graph.aget_state(config)
            if state.next:
                interrupt_value = state.tasks[0].interrupts[0].value if state.tasks else "Waiting for input."
                logger.info("graph_interrupted", session_id=session_id, interrupt_value=str(interrupt_value))
                return [Message(role="assistant", content=str(interrupt_value))]

            openai_msgs = cast(list[dict], convert_to_openai_messages(response["messages"]))
            await task_manager.schedule(
                memory_service.add(user_id, openai_msgs, config.get("metadata")),
                name="memory_add",
            )
            total_latency_ms = round((time.perf_counter() - t0) * 1000)
            logger.info(
                "chat_response_latency",
                session_id=session_id,
                memory_latency_ms=memory_latency_ms,
                total_latency_ms=total_latency_ms,
            )
            result_msgs = self.__process_messages(response["messages"])
            # Non-stream mirror of the streaming disclaimer: append the
            # hardcoded compliance notice to the last assistant message when
            # the user turn was financial (symbol hit or financial keyword).
            # Skipped for CASUAL chit-chat and interrupted turns (the early
            # return above). Gated by ENABLE_CHAT_DISCLAIMER (default off).
            if settings.ENABLE_CHAT_DISCLAIMER and result_msgs and needs_disclaimer(messages[-1].content):
                result_msgs[-1].content += DISCLAIMER_TEXT
            return result_msgs
        except GraphInterrupt:
            state = await graph.aget_state(config)
            interrupt_value = state.tasks[0].interrupts[0].value if state.tasks else "Waiting for input."
            logger.info("graph_interrupted", session_id=session_id, interrupt_value=str(interrupt_value))
            return [Message(role="assistant", content=str(interrupt_value))]
        except Exception as e:
            logger.exception("get_response_failed", error=str(e), session_id=session_id)
            raise

    async def get_stream_response(
        self,
        messages: list[Message],
        session_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        images: Optional[list[ChatImage]] = None,
    ) -> AsyncGenerator[Union[str, dict], None]:
        """Get a stream response from the LLM.

        Args:
            messages (list[Message]): The messages to send to the LLM.
            session_id (str): The session ID for the conversation.
            user_id (Optional[str]): The user ID for the conversation.
            username (Optional[str]): The display name of the user.
            images (Optional[list[ChatImage]]): Uploaded images for vision context.

        Yields:
            str: Tokens of the LLM response.
        """
        callbacks: list[BaseCallbackHandler] = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
        config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            "callbacks": callbacks,
            "metadata": {
                "user_id": user_id,
                "username": username,
                "session_id": session_id,
                "environment": settings.ENVIRONMENT.value,
                "debug": settings.DEBUG,
            },
        }
        graph = await self._get_graph()

        try:
            t0 = time.perf_counter()
            # Run state check and memory search concurrently to save 200-500ms
            state, relevant_memory = await asyncio.gather(
                graph.aget_state(config),
                memory_service.search(user_id, messages[-1].content),
            )
            memory_latency_ms = round((time.perf_counter() - t0) * 1000)

            vision_context = await self._resolve_vision_context(images, user_id, session_id)

            if state.next:
                logger.info("resuming_interrupted_graph_stream", session_id=session_id, next_nodes=state.next)
                graph_input = Command(resume=messages[-1].content)
            else:
                relevant_memory = relevant_memory or "No relevant memory found."
                graph_input = {
                    "messages": dump_messages(messages),
                    "long_term_memory": relevant_memory,
                    "vision_context": vision_context or "",
                }

            # Pre-compute intent classification OUTSIDE the graph to prevent
            # the local 1.5B model's output from leaking into the SSE stream.
            # stream_mode="messages" captures ALL LLM calls inside graph nodes,
            # so classify_chat_intent() must run before graph.astream().
            precomputed_intent: IntentResult | None = None
            if settings.ENABLE_CHAT_MODEL_ROUTING and not state.next:
                last_user_text = _extract_last_user_text(dump_messages(messages))
                precomputed_intent = await classify_chat_intent(last_user_text)

            # Surface tool calls as progress chips: collect them into a queue
            # via a callback and interleave them with the text-token stream.
            tool_event_queue: asyncio.Queue = asyncio.Queue()
            collector = _ToolEventCollector(tool_event_queue)
            config = {**config, "callbacks": [*callbacks, collector]}
            if precomputed_intent is not None:
                config["metadata"] = {**config["metadata"], "intent_result": precomputed_intent}

            async def _produce_tokens() -> None:
                try:
                    async for token, _ in graph.astream(
                        graph_input,
                        config,
                        stream_mode="messages",
                    ):
                        if not isinstance(token, (AIMessage, AIMessageChunk)):
                            continue
                        text = extract_text_content(token.content)
                        if text:
                            await token_q.put(text)
                except Exception as exc:  # surface to the consumer below
                    await token_q.put(exc)
                finally:
                    await token_q.put(None)  # sentinel: stream finished

            token_q: asyncio.Queue = asyncio.Queue()
            producer = asyncio.create_task(_produce_tokens())
            streamed_text = ""

            while True:
                get_token = asyncio.ensure_future(token_q.get())
                get_event = asyncio.ensure_future(tool_event_queue.get())
                done, pending = await asyncio.wait({get_token, get_event}, return_when=asyncio.FIRST_COMPLETED)
                for fut in pending:
                    fut.cancel()
                if get_token in done:
                    item = get_token.result()
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    if isinstance(item, str):
                        streamed_text += item
                    yield item
                else:
                    yield get_event.result()

            # Drain any trailing tool events (e.g. a final tool_end).
            while not tool_event_queue.empty():
                yield tool_event_queue.get_nowait()

            await producer

            # After streaming completes, check for interrupt or update memory
            state = await graph.aget_state(config)

            # The model often paraphrases the chart tool's output and drops the
            # Markdown image line, so the chart never renders. Re-attach it.
            if state.values and "messages" in state.values:
                _msgs = state.values["messages"]
                chart_md = _latest_chart_markdown(_msgs)
                logger.info(
                    "chart_markdown_scan",
                    found=bool(chart_md),
                    n_msgs=len(_msgs),
                    tail_types=[type(getattr(m, "content", None)).__name__ for m in _msgs[-5:]],
                )
                if chart_md and chart_md not in streamed_text:
                    logger.info(
                        "chart_markdown_injected",
                        md=chart_md,
                        streamed_len=len(streamed_text),
                    )
                    yield "\n\n" + chart_md
                else:
                    logger.info(
                        "chart_markdown_skipped",
                        md=chart_md,
                        already_in_stream=bool(chart_md) and chart_md in streamed_text,
                        streamed_len=len(streamed_text),
                    )

            if state.next:
                interrupt_value = state.tasks[0].interrupts[0].value if state.tasks else "Waiting for input."
                logger.info("graph_interrupted_stream", session_id=session_id, interrupt_value=str(interrupt_value))
                yield str(interrupt_value)
            elif state.values and "messages" in state.values:
                openai_msgs = cast(list[dict], convert_to_openai_messages(state.values["messages"]))
                await task_manager.schedule(
                    memory_service.add(user_id, openai_msgs, config.get("metadata")),
                    name="memory_add",
                )
                # Append a hardcoded compliance disclaimer when the user turn
                # was financial (symbol hit or financial keyword). Skipped for
                # CASUAL chit-chat and for interrupted turns (which ask the
                # user back rather than answer). Zero LLM cost; gated by
                # ENABLE_CHAT_DISCLAIMER (default off -> zero regression).
                if settings.ENABLE_CHAT_DISCLAIMER and needs_disclaimer(messages[-1].content):
                    yield DISCLAIMER_TEXT
            total_latency_ms = round((time.perf_counter() - t0) * 1000)
            logger.info(
                "chat_stream_latency",
                session_id=session_id,
                memory_latency_ms=memory_latency_ms,
                total_latency_ms=total_latency_ms,
            )
        except GraphInterrupt:
            state = await graph.aget_state(config)
            interrupt_value = state.tasks[0].interrupts[0].value if state.tasks else "Waiting for input."
            logger.info("graph_interrupted_stream", session_id=session_id, interrupt_value=str(interrupt_value))
            yield str(interrupt_value)
        except Exception as stream_error:
            logger.exception("stream_processing_failed", error=str(stream_error), session_id=session_id)
            raise stream_error

    async def get_chat_history(self, session_id: str) -> list[Message]:
        """Get the chat history for a given thread ID.

        Args:
            session_id (str): The session ID for the conversation.

        Returns:
            list[Message]: The chat history.
        """
        graph = await self._get_graph()

        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        state: StateSnapshot = await graph.aget_state(config=config)
        return self.__process_messages(state.values["messages"]) if state.values else []

    def __process_messages(self, messages: list[BaseMessage]) -> list[Message]:
        openai_style_messages = convert_to_openai_messages(messages)
        # keep just assistant and user messages
        return [
            Message(role=message["role"], content=str(message["content"]))
            for message in openai_style_messages
            if message["role"] in ["assistant", "user"] and message["content"]
        ]

    async def clear_chat_history(self, session_id: str, user_id: Optional[str] = None, scope: str = "session") -> None:
        """Clear chat history for a session, optionally also clearing long-term memory.

        Args:
            session_id: The ID of the session to clear history for.
            user_id: The user ID, required when ``scope="all"`` to clear
                long-term memory.
            scope: ``"session"`` (default) clears only the checkpointer;
                ``"all"`` also clears the user's long-term memory (mem0) and
                invalidates the memory semantic cache.

        Raises:
            Exception: If there's an error clearing the chat history.
        """
        try:
            # Make sure the pool is initialized in the current event loop
            conn_pool = await self._get_connection_pool()
            if conn_pool is None:
                raise RuntimeError("connection pool unavailable; cannot clear chat history")

            # Batch all DELETEs in a single pipeline round-trip
            async with conn_pool.connection() as conn:
                async with conn.pipeline():
                    for table in settings.CHECKPOINT_TABLES:
                        await conn.execute(
                            sql.SQL("DELETE FROM {} WHERE thread_id = %s").format(sql.Identifier(table)),
                            (session_id,),
                        )
                logger.info(
                    "checkpoint_tables_cleared_for_session",
                    tables=settings.CHECKPOINT_TABLES,
                    session_id=session_id,
                )

            if scope == "all" and user_id:
                await memory_service.clear_user_memory(user_id)
                logger.info(
                    "long_term_memory_cleared_for_user",
                    user_id=user_id,
                    session_id=session_id,
                )

        except Exception as e:
            logger.error(
                "clear_chat_history_operation_failed",
                session_id=session_id,
                error=str(e),
            )
            raise
