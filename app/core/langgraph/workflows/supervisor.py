"""Supervisor workflow — SDLC Orchestrator graph composer.

This module assembles the multi-agent SDLC workflow from the individual
agent node factories in :mod:`app.core.langgraph.agents`.  It owns the
graph lifecycle (compile, checkpoint, invoke, stream) and the public API
consumed by ``app.api.v1.sdlc``.

Key changes from the legacy monolithic ``graphs/supervisor.py``:

1. **Node functions delegated to ``agents/``** — each node is a factory
   function capturing an :class:`AgentContext`, not a method on this class.
2. **Shared connection pool** — uses ``app.core.db.pool.init_pool()``
   (aligned with ``LangGraphAgent``), eliminating the duplicate private
   pool.
3. **Deduplicated state building** — ``_build_initial_state`` and
   ``_process_images`` extracted from the former ``get_response`` /
   ``stream_events`` near-duplicate blocks.
4. **Tool registry** — tool lookups go through ``ctx.registry`` instead of
   the former ``self.sdlc_tools_by_name`` dict.
"""

import asyncio
import base64
import os
import sys
import time
from typing import Any, AsyncGenerator, List, Optional, cast

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import convert_to_openai_messages
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.config import Environment, settings
from app.core.db.pool import init_pool
from app.core.langgraph.agents import (
    AgentContext,
    build_default_context,
    make_coder_node,
    make_planner_node,
    make_po_node,
    make_reviewer_node,
    make_supervisor_node,
    make_tester_node,
)
from app.core.langgraph.agents.context import get_node_metadata, wrap_node_with_handoff
from app.core.logging import logger
from app.core.metrics import sdlc_workflow_completed_total, sdlc_workflow_duration_seconds
from app.core.observability import langfuse_callback_handler
from app.core.task_manager import task_manager
from app.schemas import SDLCState
from app.services.image_service import image_service
from app.services.memory import memory_service
from app.utils.errors import safe_stream_error


class SupervisorGraph:
    """Manages the SDLC Orchestrator multi-agent workflow.

    Public API (consumed by ``app.api.v1.sdlc``):
        - :meth:`get_response`
        - :meth:`stream_events`
        - :meth:`approve_task`
        - :meth:`reject_task`
    """

    def __init__(self) -> None:
        """Initialize the SDLC Supervisor with necessary components."""
        self._ctx: AgentContext = build_default_context()
        self._graph: Optional[CompiledStateGraph] = None
        logger.info(
            "sdlc_supervisor_initialized",
            model=settings.DEFAULT_LLM_MODEL,
            environment=settings.ENVIRONMENT.value,
        )

    # ------------------------------------------------------------------
    # Graph lifecycle
    # ------------------------------------------------------------------

    async def create_graph(self) -> Optional[CompiledStateGraph]:
        """Create and configure the SDLC Supervisor graph."""
        if self._graph is None:
            try:
                graph_builder = StateGraph(SDLCState)

                graph_builder.add_node(
                    "supervisor",
                    wrap_node_with_handoff("supervisor", make_supervisor_node(self._ctx)),
                )
                graph_builder.add_node(
                    "po",
                    wrap_node_with_handoff("po", make_po_node(self._ctx)),
                )
                graph_builder.add_node(
                    "planner",
                    wrap_node_with_handoff("planner", make_planner_node(self._ctx)),
                )
                graph_builder.add_node(
                    "coder",
                    wrap_node_with_handoff("coder", make_coder_node(self._ctx)),
                )
                graph_builder.add_node(
                    "reviewer",
                    wrap_node_with_handoff("reviewer", make_reviewer_node(self._ctx)),
                )
                graph_builder.add_node(
                    "tester",
                    wrap_node_with_handoff("tester", make_tester_node(self._ctx)),
                )

                graph_builder.set_entry_point("supervisor")

                graph_builder.add_edge("po", "supervisor")
                graph_builder.add_edge("planner", "supervisor")
                graph_builder.add_edge("coder", "supervisor")
                graph_builder.add_edge("reviewer", "supervisor")
                graph_builder.add_edge("tester", "supervisor")

                # 初始化 Postgres checkpointer；失败时按环境降级：
                # - 非生产环境一律降级为内存版（MemorySaver），保证流程可运行；
                # - 生产环境若显式设置 SDLC_EVAL_LOCAL=1（本地无 PG 的评测场景），
                #   同样降级为 MemorySaver，避免整体失败（生产正常 PG 可用时不触发）。
                checkpointer = None
                try:
                    if sys.platform == "win32":
                        import psycopg

                        conn = psycopg.connect(
                            host=settings.POSTGRES_HOST,
                            port=settings.POSTGRES_PORT,
                            user=settings.POSTGRES_USER,
                            password=settings.POSTGRES_PASSWORD,
                            dbname=settings.POSTGRES_DB,
                            connect_timeout=10,
                        )
                        checkpointer = AsyncPostgresSaver(conn)
                        await checkpointer.setup()
                    else:
                        connection_pool = await init_pool()
                        if connection_pool:
                            checkpointer = AsyncPostgresSaver(connection_pool)
                            await checkpointer.setup()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sdlc_checkpointer_init_failed_falling_back",
                        error=str(exc)[:200],
                        fallback="memory",
                    )
                    checkpointer = None

                if checkpointer is None:
                    local_eval = os.getenv("SDLC_EVAL_LOCAL", "0") == "1"
                    if settings.ENVIRONMENT != Environment.PRODUCTION or local_eval:
                        checkpointer = MemorySaver()
                        logger.info("sdlc_checkpointer_using_memory_saver")
                    elif settings.ENVIRONMENT == Environment.PRODUCTION:
                        raise Exception("Postgres checkpointer initialization failed")

                self._graph = graph_builder.compile(
                    checkpointer=checkpointer,
                    name=f"{settings.PROJECT_NAME} SDLC Orchestrator ({settings.ENVIRONMENT.value})",
                )

                logger.info(
                    "sdlc_graph_created",
                    has_checkpointer=checkpointer is not None,
                    environment=settings.ENVIRONMENT.value,
                )
            except Exception as e:
                logger.error("sdlc_graph_creation_failed", error=str(e))
                if settings.ENVIRONMENT == Environment.PRODUCTION:
                    logger.warning("sdlc_continue_without_graph")
                    return None
                raise e

        return self._graph

    async def _get_graph(self) -> CompiledStateGraph:
        if self._graph is None:
            self._graph = await self.create_graph()
        if self._graph is None:
            raise RuntimeError("SDLC graph initialization failed")
        return self._graph

    # ------------------------------------------------------------------
    # Shared helpers (extracted from former get_response / stream_events)
    # ------------------------------------------------------------------

    def _build_initial_state(
        self,
        requirements: str,
        relevant_memory: str,
        project_context: str = "",
        user_id: str = "",
        coder_model: Optional[str] = None,
    ) -> SDLCState:
        """Build the initial SDLCState for a new workflow invocation.

        Extracted from the near-duplicate blocks in the former
        ``get_response`` and ``stream_events`` methods.

        C-4 fix: include ``security_issues`` so the ``add`` reducer never
        sees None + list when reviewer_node first appends.
        C-5 fix: persist ``project_context`` so coder/reviewer prompts can
        read it from state (previously dropped on the floor).
        C-6 fix: persist ``user_id`` so coder_node's knowledge-base search
        guard (``if settings.ENABLE_KNOWLEDGE_BASE and user_id``) can fire.
        """
        return cast(
            SDLCState,
            {
                "messages": [{"role": "user", "content": requirements}],
                "long_term_memory": relevant_memory,
                "requirements": requirements,
                "project_context": project_context,
                "user_id": user_id,
                # PO agent's structured spec — empty until po_node runs.
                # _force_route guard uses (requirements && !product_spec) → po.
                "product_spec": [],
                "plan": [],
                "code_changes": [],
                "test_results": [],
                "feedback": [],
                "security_issues": [],
                "errors": [],
                "artifacts": [],
                "image_contexts": [],
                "human_approval_needed": False,
                "status": "planning",
                "is_complete": False,
                "validation_results": [],
                "coder_retry_count": 0,
                # Per-task coder model override; None => router default.
                "coder_model": coder_model,
            },
        )

    async def _process_images(self, images: List[str], session_id: str) -> list[dict]:
        """Download/decode and analyze images, returning image contexts.

        Extracted from the near-duplicate blocks in the former
        ``get_response`` and ``stream_events`` methods.
        """
        if not images or not settings.ENABLE_MULTIMODAL:
            return []

        if len(images) > settings.MAX_IMAGES_PER_TASK:
            logger.warning("too_many_images", session_id=session_id, count=len(images))
            images = images[: settings.MAX_IMAGES_PER_TASK]

        image_contexts: list[dict] = []
        for img in images:
            try:
                if img.startswith("http://") or img.startswith("https://"):
                    image_id = await image_service.save_image_from_url(img, session_id)
                else:
                    image_data = base64.b64decode(img)
                    image_id = await image_service.save_uploaded_image(image_data, session_id)

                analysis = await image_service.analyze_image(image_id, session_id)
                image_contexts.append(
                    {
                        "image_id": image_id,
                        "description": analysis.description,
                        "key_elements": analysis.key_elements,
                        "suggestions": analysis.suggestions,
                    }
                )
            except Exception as e:
                logger.error("image_upload_failed", session_id=session_id, error=str(e))

        return image_contexts

    def _build_config(self, session_id: str, user_id: Optional[str], step: str) -> RunnableConfig:
        """Build the LangGraph runnable config for an invocation."""
        callbacks: list[BaseCallbackHandler] = [langfuse_callback_handler] if settings.LANGFUSE_TRACING_ENABLED else []
        return {
            "configurable": {"thread_id": session_id},
            "callbacks": callbacks,
            "recursion_limit": 50,
            "metadata": {
                "user_id": user_id,
                "session_id": session_id,
                "environment": settings.ENVIRONMENT.value,
                "debug": settings.DEBUG,
                **get_node_metadata("workflow", step),
            },
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_response(
        self,
        requirements: str,
        session_id: str,
        user_id: Optional[str] = None,
        project_context: Optional[str] = None,
        images: Optional[List[str]] = None,
        coder_model: Optional[str] = None,
    ) -> SDLCState:
        """Get a response from the SDLC Orchestrator."""
        start_time = time.time()
        graph = await self._get_graph()
        config = self._build_config(session_id, user_id, "start")

        try:
            state, relevant_memory = await asyncio.gather(
                graph.aget_state(config),
                memory_service.search(user_id, requirements),
            )

            relevant_memory = relevant_memory or ""

            if state.next:
                logger.info("sdlc_resuming_interrupted_graph", session_id=session_id)
                # M-4 fix: pass None to resume from the last checkpoint
                # without injecting a duplicate user message. Previously
                # this passed {"messages": [...]} which added a second
                # copy of the requirements to the checkpoint's message
                # list on every resume (e.g. after approval).
                # S-5 fix: bound the whole graph invocation with a hard timeout
                # so a misbehaving model (e.g. hanging structured-output calls)
                # cannot pin the task indefinitely.
                response = await asyncio.wait_for(
                    graph.ainvoke(None, config=config),
                    timeout=settings.SDLC_WORKFLOW_TIMEOUT_SEC,
                )
            else:
                initial_state = self._build_initial_state(
                    requirements,
                    relevant_memory,
                    project_context=project_context or "",
                    user_id=user_id or "",
                    coder_model=coder_model,
                )
                initial_state["image_contexts"] = await self._process_images(images or [], session_id)
                response = await asyncio.wait_for(
                    graph.ainvoke(initial_state, config=config),
                    timeout=settings.SDLC_WORKFLOW_TIMEOUT_SEC,
                )

            workflow_duration = time.time() - start_time
            is_complete = response.get("is_complete", False)
            workflow_status = "success" if is_complete else "in_progress"

            sdlc_workflow_duration_seconds.labels(status=workflow_status).observe(workflow_duration)

            state = await graph.aget_state(config)
            if state.next:
                interrupt_value = state.tasks[0].interrupts[0].value if state.tasks else "Waiting for input."
                logger.info(
                    "sdlc_graph_interrupted",
                    session_id=session_id,
                    interrupt_value=str(interrupt_value),
                )

            # Convert LangChain messages to OpenAI dict format before passing
            # to mem0 — mem0's parse_vision_messages expects subscriptable
            # dicts (msg["content"]), not LangChain message objects. Mirrors
            # the pattern in graph.py:310 (chat workflow).
            openai_msgs = cast(list[dict], convert_to_openai_messages(response["messages"]))
            await task_manager.schedule(
                memory_service.add(user_id, openai_msgs, config.get("metadata")),
                name="memory_add",
            )
            return cast(SDLCState, response)

        except GraphInterrupt:
            workflow_duration = time.time() - start_time
            sdlc_workflow_duration_seconds.labels(status="interrupted").observe(workflow_duration)

            state = await graph.aget_state(config)
            interrupt_value = state.tasks[0].interrupts[0].value if state.tasks else "Waiting for input."
            logger.info(
                "sdlc_graph_interrupted",
                session_id=session_id,
                interrupt_value=str(interrupt_value),
            )
            return cast(SDLCState, state.values) if state.values else cast(SDLCState, {})
        except Exception as e:
            workflow_duration = time.time() - start_time
            sdlc_workflow_duration_seconds.labels(status="fail").observe(workflow_duration)
            sdlc_workflow_completed_total.labels(status="fail", task_type="general").inc()

            logger.exception("sdlc_get_response_failed", error=str(e), session_id=session_id)
            raise

    async def stream_events(
        self,
        requirements: str,
        session_id: str,
        user_id: Optional[str] = None,
        project_context: Optional[str] = None,
        images: Optional[List[str]] = None,
        coder_model: Optional[str] = None,
    ) -> AsyncGenerator[Any, None]:
        """Stream events from the SDLC Orchestrator."""
        start_time = time.time()
        graph = await self._get_graph()
        config = self._build_config(session_id, user_id, "stream")

        try:
            state, relevant_memory = await asyncio.gather(
                graph.aget_state(config),
                memory_service.search(user_id, requirements),
            )

            relevant_memory = relevant_memory or ""

            if state.next:
                logger.info("sdlc_stream_resuming_interrupted_graph", session_id=session_id)
                # M-4 fix: pass None to resume without duplicating the
                # user message in the checkpoint (same fix as get_response).
                async for event in graph.astream_events(
                    None,
                    config=config,
                    version="v2",
                ):
                    yield event
            else:
                initial_state = self._build_initial_state(
                    requirements,
                    relevant_memory,
                    project_context=project_context or "",
                    user_id=user_id or "",
                    coder_model=coder_model,
                )
                initial_state["image_contexts"] = await self._process_images(images or [], session_id)

                async for event in graph.astream_events(
                    initial_state,
                    config=config,
                    version="v2",
                ):
                    yield event

        except GraphInterrupt:
            # C-7 part 2 fix: GraphInterrupt is NOT an error — it's the
            # human-in-the-loop approval gate triggered by supervisor_node
            # when human_approval_needed=True. Previously this fell into the
            # generic except below, surfacing to the UI as an "error" event
            # which hid the approval buttons. Now we emit a structured
            # "approval_needed" event so the UI can render the
            # Approve/Reject controls. The reviewer's state update
            # (feedback, security_issues, human_approval_needed=True) is
            # already persisted in the checkpoint by this point.
            workflow_duration = time.time() - start_time
            sdlc_workflow_duration_seconds.labels(status="interrupted").observe(workflow_duration)
            try:
                interrupt_state = await graph.aget_state(config)
                interrupt_value = (
                    interrupt_state.tasks[0].interrupts[0].value if interrupt_state.tasks else "Waiting for approval."
                )
            except Exception:
                interrupt_value = "Waiting for approval."
            logger.info(
                "sdlc_stream_approval_needed",
                session_id=session_id,
                interrupt_value=str(interrupt_value),
            )
            yield {
                "event": "approval_needed",
                "data": {
                    "message": str(interrupt_value),
                    "thread_id": session_id,
                },
            }
            return
        except Exception as e:
            # C-9 fix: sanitize the error message before yielding so DB
            # connection strings, filesystem paths, etc. never reach the
            # client via the SSE passthrough in sdlc.py stream_response.
            # M-5 fix: do NOT re-raise — re-raising causes sdlc.py's
            # stream_response except block to fire and yield a SECOND
            # error event, so the client sees two errors for one fault.
            # The error is already logged by safe_stream_error; just
            # return after yielding the single sanitized error event.
            safe_msg = safe_stream_error(e, context="sdlc_stream_events")
            yield {"event": "error", "data": {"error": safe_msg}}
            return

    async def approve_task(self, session_id: str) -> SDLCState:
        """Approve a task that requires human approval."""
        graph = await self._get_graph()
        # M-6 fix: use _build_config so approve/reject get the same
        # callbacks (Langfuse tracing), metadata, and recursion_limit as
        # the main workflow. Previously the bare config omitted all
        # three, causing untraced approvals and potential recursion_limit
        # exhaustion on complex resumes.
        config = self._build_config(session_id, None, "approve")

        try:
            state = await graph.aget_state(config)
            if not state.next:
                logger.warning("sdlc_no_pending_task_to_approve", session_id=session_id)
                return cast(SDLCState, state.values) if state.values else cast(SDLCState, {})

            graph.update_state(
                config,
                {"human_approval_needed": False},
                as_node="supervisor",
            )

            logger.info("sdlc_task_approved", session_id=session_id)
            response = await graph.ainvoke(None, config=config)

            if response.get("is_complete", False):
                sdlc_workflow_completed_total.labels(status="approved", task_type="general").inc()

            return cast(SDLCState, response)

        except Exception as e:
            logger.exception("sdlc_approve_task_failed", error=str(e), session_id=session_id)
            raise

    async def reject_task(self, session_id: str, reason: str = "") -> SDLCState:
        """Reject a task that requires human approval."""
        graph = await self._get_graph()
        # M-6 fix: same as approve_task — use _build_config for full config.
        config = self._build_config(session_id, None, "reject")

        try:
            state = await graph.aget_state(config)
            if not state.next:
                logger.warning("sdlc_no_pending_task_to_reject", session_id=session_id)
                return cast(SDLCState, state.values) if state.values else cast(SDLCState, {})

            graph.update_state(
                config,
                {
                    "human_approval_needed": False,
                    "errors": [
                        {
                            "agent_name": "human",
                            "content": f"Task rejected: {reason}" if reason else "Task rejected by human",
                            "severity": "high",
                        }
                    ],
                },
                as_node="supervisor",
            )

            logger.info("sdlc_task_rejected", session_id=session_id, reason=reason)
            response = await graph.ainvoke(None, config=config)

            sdlc_workflow_completed_total.labels(status="rejected", task_type="general").inc()

            return cast(SDLCState, response)

        except Exception as e:
            logger.exception("sdlc_reject_task_failed", error=str(e), session_id=session_id)
            raise

    async def get_thread_requirements(self, session_id: str) -> Optional[str]:
        """Read the original requirements from a thread's checkpoint state.

        Used by the replay endpoint to start a fresh workflow with the same
        requirements on a new thread_id, without copying checkpoint state,
        workspace files, or code index. The source thread is not affected.

        Args:
            session_id: The source thread ID to read requirements from.

        Returns:
            The requirements string if the thread has state, ``None``
            otherwise (e.g. thread never orchestrated or checkpoint purged).
        """
        graph = await self._get_graph()
        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        try:
            state = await graph.aget_state(config)
            if not state.values:
                return None
            return cast(Optional[str], state.values.get("requirements"))
        except Exception as e:
            logger.exception(
                "sdlc_get_thread_requirements_failed",
                error=str(e),
                session_id=session_id,
            )
            return None

    async def get_state(self, session_id: str) -> SDLCState:
        """Read the full SDLC state for a thread from its checkpoint.

        Used by the ``GET /api/v1/sdlc/{thread_id}/state`` endpoint so the
        UI can render the final ``code_changes`` / ``plan`` / ``messages``
        / ``is_complete`` after the SSE stream ends — the astream_events
        ``on_chain_end`` events wrap node outputs in ``Command`` objects
        whose structure the UI should not have to parse.

        Args:
            session_id: The thread ID to read state from.

        Returns:
            The full SDLCState dict (empty if the thread has no state).
        """
        graph = await self._get_graph()
        config: RunnableConfig = {"configurable": {"thread_id": session_id}}
        try:
            state = await graph.aget_state(config)
            if not state.values:
                return cast(SDLCState, {})
            return cast(SDLCState, state.values)
        except Exception as e:
            logger.exception(
                "sdlc_get_state_failed",
                error=str(e),
                session_id=session_id,
            )
            return cast(SDLCState, {})
