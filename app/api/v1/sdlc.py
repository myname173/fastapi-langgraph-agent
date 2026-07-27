"""SDLC Orchestrator API endpoints.

This module provides endpoints for the SDLC Orchestrator multi-agent workflow,
separate from the chatbot endpoints.
"""

import json
import uuid
from typing import Any, Literal, Mapping, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.v1.auth import get_current_session
from app.core.config import settings
from app.core.langgraph.tools.sdlc_tools import get_workspace_path
from app.core.langgraph.workflows import SupervisorGraph
from app.core.limiter import limiter
from app.core.logging import logger
from app.models.approval_log import ApprovalAction, ApprovalLog, ApprovalStep
from app.models.session import Session
from app.models.sdlc_task import SDLCTask, SDLCTaskStatus
from app.schemas.sdlc import SDLCReplayResponse, SDLCRequest, SDLCResponse
from app.services.approval_log_service import approval_log_service
from app.services.llm.router import model_router
from app.services.goal_service import goal_service
from app.services.sdlc_task_service import sdlc_task_service
from app.utils.errors import safe_500, safe_stream_error

SDLCStatus = Literal["planning", "coding", "testing", "reviewing", "completed"]

router = APIRouter()
orchestrator = SupervisorGraph()


def _json_default(obj: object) -> object:
    """JSON default handler for non-serializable objects (LangChain BaseMessage, etc.).

    LangGraph astream_events v2 yields events whose ``data`` may contain LangChain
    message objects (HumanMessage, AIMessage, AIMessageChunk, ToolMessage, …) that
    are not JSON-serializable by default. This handler converts them via Pydantic's
    ``model_dump()``/``dict()`` so the SSE stream can serialize the full event.
    """
    if hasattr(obj, "model_dump"):
        return cast(Any, obj).model_dump()
    if hasattr(obj, "dict"):
        return cast(Any, obj).dict()
    return str(obj)


async def _persist_task_outcome(thread_id: str, state: Mapping[str, Any]) -> None:
    """Best-effort mirror of SDLCState status + plan into the SDLCTask row.

    The LangGraph checkpoint remains the source of truth; SDLCTask is a
    queryable index for GET /tasks. Swallows DB errors so a persistence
    hiccup never breaks the workflow response (the row already exists
    from create_task at orchestrate/stream start).
    """
    try:
        status_str = state.get("status")
        if status_str:
            await sdlc_task_service.update_task_status(thread_id, SDLCTaskStatus(status_str))
        plan = state.get("plan")
        if plan:
            await sdlc_task_service.update_task_plan(thread_id, json.dumps(plan))
    except Exception as e:
        logger.warning("sdlc_task_persist_failed", thread_id=thread_id, error=str(e))


@router.post("/", response_model=SDLCResponse)
@limiter.limit("10 per minute")
async def sdlc_orchestrate(
    request: Request,
    sdlc_request: SDLCRequest,
    session: Session = Depends(get_current_session),
):
    """Orchestrate an SDLC workflow using multi-agent system."""
    # Validate user-selected coder model BEFORE entering the try block so
    # HTTPException(400) propagates to FastAPI's exception handler instead
    # of being caught by the generic 'except Exception' below (which wraps
    # everything in safe_500 -> 500, masking the intended 400). coder_node
    # calls get_llm_for(model_name) with NO fallback, so an unregistered
    # name must be rejected at the API boundary.
    if sdlc_request.coder_model and sdlc_request.coder_model not in settings.CODER_ELIGIBLE_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"coder_model 不在可选列表内: {sdlc_request.coder_model}",
        )

    try:
        # L-3 fix: generate a unique thread_id per orchestration call so
        # concurrent tasks in the same session don't share/overwrite
        # checkpoint state. Previously this was str(session.id).
        thread_id = str(uuid.uuid4())
        logger.info(
            "sdlc_request_received",
            session_id=session.id,
            thread_id=thread_id,
            requirements_length=len(sdlc_request.requirements),
        )

        # Wire SDLCTask persistence (previously dead code: create_task
        # was never called, so /tasks always returned an empty list).
        # task_id == thread_id matches the approval_log convention.
        # quota overflow raises ValueError -> isolated here as 429 so
        # SDLCResponse ValidationError (also a ValueError subclass)
        # is not misclassified as a quota error by the outer handler.
        # Goal context injection: if goal_id is provided, assemble historical
        # context from the Goal's tasks and inject into project_context.
        # This happens at the API layer — planner_node receives the context
        # via project_context without any DB I/O in the graph.
        project_context = sdlc_request.project_context
        if sdlc_request.goal_id:
            try:
                goal_context = await goal_service.build_context(sdlc_request.goal_id, session.user_id)
                if goal_context:
                    if project_context:
                        project_context = f"{project_context}\n\n---\n\n{goal_context}"
                    else:
                        project_context = goal_context
                    logger.info(
                        "goal_context_injected",
                        goal_id=sdlc_request.goal_id,
                        context_length=len(project_context),
                    )
            except Exception as e:
                logger.warning("goal_context_build_failed", goal_id=sdlc_request.goal_id, error=str(e))

        try:
            await sdlc_task_service.create_task(
                task_id=thread_id,
                thread_id=thread_id,
                user_id=session.user_id,
                requirements=sdlc_request.requirements,
                goal_id=sdlc_request.goal_id,
            )
        except ValueError as ve:
            raise HTTPException(status_code=429, detail=str(ve))

        response = await orchestrator.get_response(
            requirements=sdlc_request.requirements,
            session_id=thread_id,
            user_id=str(session.user_id),
            project_context=project_context,
            images=sdlc_request.images,
            coder_model=sdlc_request.coder_model,
        )

        # Best-effort mirror SDLCState status + plan into SDLCTask.
        # Failures here must not break the response (checkpoint is the
        # source of truth); _persist_task_outcome logs and swallows.
        await _persist_task_outcome(thread_id, response)

        last_message = ""
        messages = response["messages"]
        if messages:
            last_msg = messages[-1]
            if isinstance(last_msg, dict) and "content" in last_msg:
                last_message = str(last_msg["content"])

        logger.info("sdlc_request_processed", session_id=session.id, thread_id=thread_id)

        return SDLCResponse(
            status=cast(SDLCStatus, response.get("status", "planning")),
            plan=cast(list, response.get("plan", [])),
            code_changes=cast(list, response.get("code_changes", [])),
            test_results=cast(list, response.get("test_results", [])),
            feedback=cast(list, response.get("feedback", [])),
            security_issues=cast(list, response.get("security_issues", [])),
            is_complete=cast(bool, response.get("is_complete", False)),
            message=last_message,
            human_approval_needed=cast(bool, response.get("human_approval_needed", False)),
            thread_id=thread_id,
        )

    except HTTPException:
        # Re-raise HTTPException (e.g. the 429 quota error raised by
        # create_task above) so it is not wrapped as safe_500 by the
        # generic handler below. SDLCResponse ValidationError (a
        # ValueError subclass) is NOT caught here — it falls through
        # to except Exception -> safe_500, which is the correct 500.
        raise
    except Exception as e:
        logger.exception("sdlc_request_failed", session_id=session.id, error=str(e))
        raise safe_500(e, context="sdlc_orchestrate")


@router.post("/stream")
@limiter.limit("5 per minute")
async def sdlc_orchestrate_stream(
    request: Request,
    sdlc_request: SDLCRequest,
    session: Session = Depends(get_current_session),
):
    """Orchestrate an SDLC workflow with streaming response.

    Streams events in real-time as the workflow progresses through each agent.
    """
    # Validate coder_model BEFORE starting the SSE stream so an invalid
    # name returns HTTP 400 immediately (matching POST /). Validating
    # inside stream_response() would yield a 200 + SSE error event,
    # which the UI handles less gracefully.
    if sdlc_request.coder_model and sdlc_request.coder_model not in settings.CODER_ELIGIBLE_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"coder_model 不在可选列表内: {sdlc_request.coder_model}",
        )

    async def stream_response():
        try:
            # L-3 fix: generate a unique thread_id per orchestration call.
            # Emit it as the first event so the UI can capture it for
            # subsequent /state, /approve, /reject calls.
            thread_id = str(uuid.uuid4())
            logger.info(
                "sdlc_stream_request_received",
                session_id=session.id,
                thread_id=thread_id,
                requirements_length=len(sdlc_request.requirements),
            )

            # Emit thread_id first so the UI can store it before any
            # workflow events arrive.
            yield f"data: {json.dumps({'event': 'thread_started', 'data': {'thread_id': thread_id}})}\n\n"

            # Wire SDLCTask persistence (previously dead code). On quota
            # overflow, yield a clear error event and stop — do NOT run the
            # workflow without a task row (that would re-create the empty
            # /tasks bug). task_id == thread_id per approval_log convention.
            # Goal context injection (same as POST /).
            project_context = sdlc_request.project_context
            if sdlc_request.goal_id:
                try:
                    goal_context = await goal_service.build_context(sdlc_request.goal_id, session.user_id)
                    if goal_context:
                        if project_context:
                            project_context = f"{project_context}\n\n---\n\n{goal_context}"
                        else:
                            project_context = goal_context
                        logger.info(
                            "goal_context_injected",
                            goal_id=sdlc_request.goal_id,
                            context_length=len(project_context),
                        )
                except Exception as e:
                    logger.warning("goal_context_build_failed", goal_id=sdlc_request.goal_id, error=str(e))

            try:
                await sdlc_task_service.create_task(
                    task_id=thread_id,
                    thread_id=thread_id,
                    user_id=session.user_id,
                    requirements=sdlc_request.requirements,
                    goal_id=sdlc_request.goal_id,
                )
            except ValueError as ve:
                yield f"data: {json.dumps({'event': 'error', 'data': {'error': str(ve)}})}\n\n"
                return

            async for event in orchestrator.stream_events(
                requirements=sdlc_request.requirements,
                session_id=thread_id,
                user_id=str(session.user_id),
                project_context=project_context,
                images=sdlc_request.images,
                coder_model=sdlc_request.coder_model,
            ):
                yield f"data: {json.dumps(event, default=_json_default)}\n\n"

            # Stream ended (normal completion / approval_needed / error all
            # return inside stream_events, so the async-for always ends
            # cleanly). Mirror the final checkpoint state into SDLCTask.
            final_state = await orchestrator.get_state(session_id=thread_id)
            await _persist_task_outcome(thread_id, final_state)

        except Exception as e:
            # C-9 fix: never leak raw str(e) to the client — it can
            # contain DB connection strings, filesystem paths, and
            # internal stack details. safe_stream_error logs the full
            # exception server-side (with request_id for correlation)
            # and returns a sanitized message: str(e) in dev/test, a
            # generic "Internal server error (request_id=...)" in
            # staging/production.
            safe_msg = safe_stream_error(e, context="sdlc_stream")
            yield f"data: {json.dumps({'event': 'error', 'data': {'error': safe_msg}})}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")


@router.get("/{thread_id}/state")
@limiter.limit("20 per minute")
async def sdlc_get_state(
    request: Request,
    thread_id: str,
    session: Session = Depends(get_current_session),
):
    """Get the full SDLC state for a thread.

    Returns the complete SDLCState (code_changes, plan, messages,
    is_complete, human_approval_needed, ...) from the LangGraph
    checkpoint. The UI calls this after the SSE stream ends to render
    the final results, because astream_events ``on_chain_end`` events
    wrap node outputs in ``Command`` objects the UI should not parse.
    """
    try:
        state = await orchestrator.get_state(session_id=thread_id)
        logger.info(
            "sdlc_state_request_processed",
            thread_id=thread_id,
            session_id=session.id,
            is_complete=state.get("is_complete", False),
            code_changes_count=len(state.get("code_changes", [])),
        )
        return state
    except Exception as e:
        logger.exception("sdlc_state_request_failed", thread_id=thread_id, error=str(e))
        raise safe_500(e, context="sdlc_get_state")


@router.get("/feature-flags")
@limiter.limit("30 per minute")
async def sdlc_get_feature_flags(
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Return the actual feature flag values from server settings.

    ISSUE-9 fix: the UI previously only displayed flag names without
    knowing which features are actually enabled. This endpoint exposes
    the real values from ``settings`` so the sidebar can show
    enabled/disabled status.
    """
    try:
        return {
            "ENABLE_SMART_CODER": settings.ENABLE_SMART_CODER,
            "ENABLE_MULTIMODAL": settings.ENABLE_MULTIMODAL,
            "ENABLE_SCRIPT_EXECUTION": settings.ENABLE_SCRIPT_EXECUTION,
            "ENABLE_RESOURCE_QUOTA": settings.ENABLE_RESOURCE_QUOTA,
            "ENABLE_SMART_SEARCH": settings.ENABLE_SMART_SEARCH,
            "ENABLE_KNOWLEDGE_GRAPH": settings.ENABLE_KNOWLEDGE_GRAPH,
            "ENABLE_SMART_EDITING": settings.ENABLE_SMART_EDITING,
            "ENABLE_CODE_INDEX": settings.CODE_INDEX_ENABLED,
        }
    except Exception as e:
        logger.exception("sdlc_feature_flags_failed", error=str(e))
        raise safe_500(e, context="sdlc_get_feature_flags")


@router.get("/models")
@limiter.limit("30 per minute")
async def sdlc_get_models(
    request: Request,
    session: Session = Depends(get_current_session),
):
    """List LLM models the user may select for the coder agent.

    Returns the CODER_ELIGIBLE_MODELS list (validated at config load
    time) plus the router default so the UI can mark the default
    option. The coder_node consumes SDLCRequest.coder_model directly
    with no fallback, so every entry here MUST be resolvable by
    LLMRegistry.get() at runtime.
    """
    return {
        "models": list(settings.CODER_ELIGIBLE_MODELS),
        "default": model_router.get_model_for("coder"),
    }


@router.post("/{thread_id}/approve", response_model=SDLCResponse)
@limiter.limit("5 per minute")
async def sdlc_approve(
    request: Request,
    thread_id: str,
    session: Session = Depends(get_current_session),
):
    """Approve a task that requires human approval."""
    try:
        logger.info("sdlc_approve_request_received", thread_id=thread_id, session_id=session.id)

        response = await orchestrator.approve_task(session_id=thread_id)

        # Mirror the resumed workflow status into SDLCTask (best-effort).
        # The task row was created when the original /stream run started.
        await _persist_task_outcome(thread_id, response)

        await approval_log_service.log_approval(
            task_id=thread_id,
            step=ApprovalStep.REVIEWER,
            action=ApprovalAction.APPROVE,
            user_id=session.user_id,
            state=response,
            status_before="reviewing",
            status_after=response.get("status", "completed"),
        )

        last_message = ""
        messages = response["messages"]
        if messages:
            last_msg = messages[-1]
            if isinstance(last_msg, dict) and "content" in last_msg:
                last_message = str(last_msg["content"])

        logger.info("sdlc_approve_request_processed", thread_id=thread_id)

        return SDLCResponse(
            status=cast(SDLCStatus, response.get("status", "planning")),
            plan=cast(list, response.get("plan", [])),
            code_changes=cast(list, response.get("code_changes", [])),
            test_results=cast(list, response.get("test_results", [])),
            feedback=cast(list, response.get("feedback", [])),
            security_issues=cast(list, response.get("security_issues", [])),
            is_complete=cast(bool, response.get("is_complete", False)),
            message=last_message,
            human_approval_needed=cast(bool, response.get("human_approval_needed", False)),
        )

    except Exception as e:
        logger.exception("sdlc_approve_request_failed", thread_id=thread_id, error=str(e))
        raise safe_500(e, context="sdlc_approve")


@router.post("/{thread_id}/reject", response_model=SDLCResponse)
@limiter.limit("5 per minute")
async def sdlc_reject(
    request: Request,
    thread_id: str,
    reason: str = Body(default="", embed=True),
    session: Session = Depends(get_current_session),
):
    """Reject a task that requires human approval."""
    try:
        logger.info(
            "sdlc_reject_request_received",
            thread_id=thread_id,
            session_id=session.id,
            reason=reason,
        )

        response = await orchestrator.reject_task(session_id=thread_id, reason=reason)

        # Mirror the resumed workflow status into SDLCTask (best-effort).
        await _persist_task_outcome(thread_id, response)

        await approval_log_service.log_approval(
            task_id=thread_id,
            step=ApprovalStep.REVIEWER,
            action=ApprovalAction.REJECT,
            user_id=session.user_id,
            reason=reason,
            state=response,
            status_before="reviewing",
            status_after=response.get("status", "planning"),
        )

        last_message = ""
        messages = response["messages"]
        if messages:
            last_msg = messages[-1]
            if isinstance(last_msg, dict) and "content" in last_msg:
                last_message = str(last_msg["content"])

        logger.info("sdlc_reject_request_processed", thread_id=thread_id)

        return SDLCResponse(
            status=cast(SDLCStatus, response.get("status", "planning")),
            plan=cast(list, response.get("plan", [])),
            code_changes=cast(list, response.get("code_changes", [])),
            test_results=cast(list, response.get("test_results", [])),
            feedback=cast(list, response.get("feedback", [])),
            security_issues=cast(list, response.get("security_issues", [])),
            is_complete=cast(bool, response.get("is_complete", False)),
            message=last_message,
            human_approval_needed=cast(bool, response.get("human_approval_needed", False)),
        )

    except Exception as e:
        logger.exception("sdlc_reject_request_failed", thread_id=thread_id, error=str(e))
        raise safe_500(e, context="sdlc_reject")


@router.post("/{thread_id}/replay", response_model=SDLCReplayResponse)
@limiter.limit("5 per minute")
async def sdlc_replay(
    request: Request,
    thread_id: str,
    session: Session = Depends(get_current_session),
):
    """Replay a task with the same requirements on a new thread.

    Reads the original requirements from the source thread's checkpoint
    and starts a fresh SDLC workflow on a new thread_id. The original
    thread is not affected (no checkpoint, workspace, or code index copy).

    This is a lightweight alternative to full checkpoint branching — the
    new branch starts from planning with the same requirements, benefiting
    from the user's long-term memory but carrying no execution history.
    """
    try:
        requirements = await orchestrator.get_thread_requirements(thread_id)
        if not requirements:
            raise HTTPException(
                status_code=404,
                detail="Source thread not found or has no requirements",
            )

        new_thread_id = str(uuid.uuid4())

        logger.info(
            "sdlc_replay_started",
            source_thread_id=thread_id,
            new_thread_id=new_thread_id,
            session_id=session.id,
        )

        response = await orchestrator.get_response(
            requirements=requirements,
            session_id=new_thread_id,
            user_id=str(session.user_id),
        )

        last_message = ""
        messages = response.get("messages", [])
        if messages:
            last_msg = messages[-1]
            if isinstance(last_msg, dict) and "content" in last_msg:
                last_message = str(last_msg["content"])

        logger.info(
            "sdlc_replay_completed",
            source_thread_id=thread_id,
            new_thread_id=new_thread_id,
        )

        return SDLCReplayResponse(
            status=cast(SDLCStatus, response.get("status", "planning")),
            plan=cast(list, response.get("plan", [])),
            code_changes=cast(list, response.get("code_changes", [])),
            test_results=cast(list, response.get("test_results", [])),
            feedback=cast(list, response.get("feedback", [])),
            security_issues=cast(list, response.get("security_issues", [])),
            is_complete=cast(bool, response.get("is_complete", False)),
            message=last_message,
            human_approval_needed=cast(bool, response.get("human_approval_needed", False)),
            new_thread_id=new_thread_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("sdlc_replay_failed", thread_id=thread_id, error=str(e))
        raise safe_500(e, context="sdlc_replay")


@router.get("/tasks", response_model=list[SDLCTask])
@limiter.limit("20 per minute")
async def sdlc_list_tasks(request: Request, session: Session = Depends(get_current_session)):
    """List all SDLC tasks for the current user."""
    try:
        tasks = await sdlc_task_service.get_tasks_by_user(session.user_id)
        logger.info("sdlc_list_tasks", user_id=session.user_id, task_count=len(tasks))
        return tasks
    except Exception as e:
        logger.exception("sdlc_list_tasks_failed", user_id=session.user_id, error=str(e))
        raise safe_500(e, context="sdlc_list_tasks")


@router.get("/tasks/{task_id}", response_model=SDLCTask)
@limiter.limit("20 per minute")
async def sdlc_get_task(task_id: str, request: Request, session: Session = Depends(get_current_session)):
    """Get a specific SDLC task by ID."""
    try:
        task = await sdlc_task_service.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        logger.info("sdlc_get_task", task_id=task_id)
        return task
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("sdlc_get_task_failed", task_id=task_id, error=str(e))
        raise safe_500(e, context="sdlc_get_task")


@router.get("/{thread_id}/approvals", response_model=list[ApprovalLog])
@limiter.limit("20 per minute")
async def sdlc_get_approvals(thread_id: str, request: Request, session: Session = Depends(get_current_session)):
    """Get approval history for a task."""
    try:
        approvals = await approval_log_service.get_approvals_by_task(thread_id)
        logger.info("sdlc_get_approvals", thread_id=thread_id, approval_count=len(approvals))
        return approvals
    except Exception as e:
        logger.exception("sdlc_get_approvals_failed", thread_id=thread_id, error=str(e))
        raise safe_500(e, context="sdlc_get_approvals")


@router.get("/{thread_id}/artifacts")
@limiter.limit("30 per minute")
async def sdlc_get_artifacts(thread_id: str, request: Request, session: Session = Depends(get_current_session)):
    """List previewable artifacts for a task.

    Reads individual JSON files from ``workspace/{thread_id}/.artifacts/``
    (one file per artifact, written by the ``register_artifact`` tool).
    Returns an empty list when no artifacts have been registered.

    Uses ``thread_id`` (not ``task_id``) for addressing because the workspace
    is physically organised by thread_id. The endpoint is co-located with
    other ``/{thread_id}/*`` routes; path patterns differ from
    ``/tasks/{task_id}`` so there is no route collision.
    """
    try:
        artifacts_dir = get_workspace_path(thread_id, ".artifacts")
        artifacts: list[dict] = []
        if artifacts_dir.exists():
            for af in sorted(artifacts_dir.glob("*.json")):
                try:
                    data = json.loads(af.read_text(encoding="utf-8"))
                    # Attach absolute URL for direct browser access.
                    # base_url has trailing "/", preview_url has leading "/".
                    preview_url = data.get("preview_url", "")
                    if preview_url:
                        data["absolute_url"] = f"{request.base_url}{preview_url.lstrip('/')}"
                    artifacts.append(data)
                except Exception:
                    # Skip malformed artifact files (corrupted / partial writes).
                    continue

        logger.info("sdlc_get_artifacts", thread_id=thread_id, count=len(artifacts))
        return {"thread_id": thread_id, "artifacts": artifacts}
    except Exception as e:
        logger.exception("sdlc_get_artifacts_failed", thread_id=thread_id, error=str(e))
        raise safe_500(e, context="sdlc_get_artifacts")


@router.post("/tasks/{task_id}/cancel")
@limiter.limit("10 per minute")
async def sdlc_cancel_task(task_id: str, request: Request, session: Session = Depends(get_current_session)):
    """Cancel an SDLC task."""
    try:
        task = await sdlc_task_service.cancel_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        logger.info("sdlc_cancel_task", task_id=task_id)
        return {"message": "Task cancelled successfully", "task": task}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("sdlc_cancel_task_failed", task_id=task_id, error=str(e))
        raise safe_500(e, context="sdlc_cancel_task")


@router.delete("/tasks/{task_id}")
@limiter.limit("10 per minute")
async def sdlc_delete_task(task_id: str, request: Request, session: Session = Depends(get_current_session)):
    """Delete an SDLC task."""
    try:
        success = await sdlc_task_service.delete_task(task_id)
        if not success:
            raise HTTPException(status_code=404, detail="Task not found")
        logger.info("sdlc_delete_task", task_id=task_id)
        return {"message": "Task deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("sdlc_delete_task_failed", task_id=task_id, error=str(e))
        raise safe_500(e, context="sdlc_delete_task")
