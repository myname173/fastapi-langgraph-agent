"""SDLC Orchestrator API endpoints.

This module provides endpoints for the SDLC Orchestrator multi-agent workflow,
separate from the chatbot endpoints.
"""

import json
import uuid
from typing import Any, Literal, cast

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
from app.models.sdlc_task import SDLCTask
from app.schemas.sdlc import SDLCReplayResponse, SDLCRequest, SDLCResponse
from app.services.approval_log_service import approval_log_service
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


@router.post("/", response_model=SDLCResponse)
@limiter.limit("10 per minute")
async def sdlc_orchestrate(
    request: Request,
    sdlc_request: SDLCRequest,
    session: Session = Depends(get_current_session),
):
    """Orchestrate an SDLC workflow using multi-agent system."""
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

        response = await orchestrator.get_response(
            requirements=sdlc_request.requirements,
            session_id=thread_id,
            user_id=str(session.user_id),
            project_context=sdlc_request.project_context,
            images=sdlc_request.images,
        )

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

            async for event in orchestrator.stream_events(
                requirements=sdlc_request.requirements,
                session_id=thread_id,
                user_id=str(session.user_id),
                project_context=sdlc_request.project_context,
                images=sdlc_request.images,
            ):
                yield f"data: {json.dumps(event, default=_json_default)}\n\n"

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
