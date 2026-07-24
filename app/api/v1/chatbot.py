"""Chatbot API endpoints for handling chat interactions.

This module provides endpoints for chat interactions, including regular chat,
streaming chat, message history management, and chat history clearing.
"""

import asyncio
import json

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import StreamingResponse

from app.api.v1.auth import get_current_session
from app.core.config import settings
from app.services.quota_service import quota_service
from app.core.langgraph.graph import LangGraphAgent
from app.core.limiter import limiter
from app.core.logging import logger
from app.core.metrics import llm_stream_duration_seconds
from app.models.session import Session
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    StreamResponse,
)
from app.services.session_naming import maybe_name_session
from app.utils.errors import safe_500, safe_stream_error

router = APIRouter()
agent = LangGraphAgent()


@router.post("/chat", response_model=ChatResponse)
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["chat"][0])
async def chat(
    request: Request,
    chat_request: ChatRequest,
    session: Session = Depends(get_current_session),
):
    """Process a chat request using LangGraph.

    Args:
        request: The FastAPI request object for rate limiting.
        chat_request: The chat request containing messages.
        session: The current session from the auth token.

    Returns:
        ChatResponse: The processed chat response.

    Raises:
        HTTPException: If there's an error processing the request.
    """
    try:
        logger.info(
            "chat_request_received",
            session_id=session.id,
            message_count=len(chat_request.messages),
        )

        if settings.SESSION_NAMING_ENABLED:
            await maybe_name_session(session.id, session.name, chat_request.messages)

        if settings.ENABLE_MULTIMODAL and chat_request.images:
            if not quota_service.check_vision_quota(session.user_id):
                raise HTTPException(
                    status_code=429,
                    detail="今日视觉分析次数已达上限，请明天再试。",
                )

        try:
            result = await asyncio.wait_for(
                agent.get_response(
                    chat_request.messages,
                    session.id,
                    user_id=str(session.user_id),
                    username=session.username,
                    images=chat_request.images,
                ),
                timeout=settings.CHAT_NONSTREAM_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "chat_request_timeout",
                session_id=session.id,
                timeout=settings.CHAT_NONSTREAM_TIMEOUT_SEC,
            )
            raise HTTPException(504, "请求超时，请改用流式对话或精简问题")

        logger.info("chat_request_processed", session_id=session.id)

        return ChatResponse(messages=result)
    except HTTPException:
        raise
    except Exception as e:
        raise safe_500(e, context="chat")


@router.post("/chat/stream")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["chat_stream"][0])
async def chat_stream(
    request: Request,
    chat_request: ChatRequest,
    session: Session = Depends(get_current_session),
):
    """Process a chat request using LangGraph with streaming response.

    Args:
        request: The FastAPI request object for rate limiting.
        chat_request: The chat request containing messages.
        session: The current session from the auth token.

    Returns:
        StreamingResponse: A streaming response of the chat completion.

    Raises:
        HTTPException: If there's an error processing the request.
    """
    try:
        logger.info(
            "stream_chat_request_received",
            session_id=session.id,
            message_count=len(chat_request.messages),
        )

        if settings.SESSION_NAMING_ENABLED:
            await maybe_name_session(session.id, session.name, chat_request.messages)

        if settings.ENABLE_MULTIMODAL and chat_request.images:
            if not quota_service.check_vision_quota(session.user_id):
                raise HTTPException(
                    status_code=429,
                    detail="今日视觉分析次数已达上限，请明天再试。",
                )

        async def event_generator():
            """Generate streaming events.

            Yields:
                str: Server-sent events in JSON format.

            Raises:
                Exception: If there's an error during streaming.
            """
            try:
                with llm_stream_duration_seconds.labels(model=agent.llm_service.get_llm().get_name()).time():
                    async for chunk in agent.get_stream_response(
                        chat_request.messages,
                        session.id,
                        user_id=str(session.user_id),
                        username=session.username,
                        images=chat_request.images,
                    ):
                        response = StreamResponse(content=chunk, done=False)
                        yield f"data: {json.dumps(response.model_dump(mode='json'))}\n\n"

                # Send final message indicating completion
                final_response = StreamResponse(content="", done=True)
                yield f"data: {json.dumps(final_response.model_dump(mode='json'))}\n\n"

            except Exception as e:
                # C-9 fix: never leak raw str(e) to the client — it can
                # contain DB connection strings, filesystem paths, and
                # internal stack details. safe_stream_error logs the full
                # exception server-side (with request_id for correlation)
                # and returns a sanitized message.
                safe_msg = safe_stream_error(e, context="chat_stream")
                error_response = StreamResponse(content=safe_msg, done=True)
                yield f"data: {json.dumps(error_response.model_dump(mode='json'))}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    except Exception as e:
        logger.exception(
            "stream_chat_request_failed",
            session_id=session.id,
            error=str(e),
        )
        raise safe_500(e, context="chat_stream")


@router.get("/messages", response_model=ChatResponse)
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["messages"][0])
async def get_session_messages(
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Get all messages for a session.

    Args:
        request: The FastAPI request object for rate limiting.
        session: The current session from the auth token.

    Returns:
        ChatResponse: All messages in the session.

    Raises:
        HTTPException: If there's an error retrieving the messages.
    """
    try:
        messages = await agent.get_chat_history(session.id)
        return ChatResponse(messages=messages)
    except Exception as e:
        logger.exception("get_messages_failed", session_id=session.id, error=str(e))
        raise safe_500(e, context="get_session_messages")


@router.delete("/messages")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["messages"][0])
async def clear_chat_history(
    request: Request,
    scope: str = "session",
    session: Session = Depends(get_current_session),
):
    """Clear chat history for the session.

    Args:
        request: The FastAPI request object for rate limiting.
        scope: ``"session"`` (default) clears only this session's checkpoint
            history; ``"all"`` also clears the user's long-term memory.
        session: The current session from the auth token.

    Returns:
        dict: A message indicating the scope that was cleared.
    """
    try:
        await agent.clear_chat_history(session.id, user_id=str(session.user_id), scope=scope)
        return {"message": "Chat history cleared successfully", "scope": scope}
    except Exception as e:
        logger.exception("clear_chat_history_failed", session_id=session.id, error=str(e))
        raise safe_500(e, context="clear_chat_history")
