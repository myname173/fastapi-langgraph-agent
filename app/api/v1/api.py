"""API v1 router configuration.

This module sets up the main API router and includes all sub-routers for different
endpoints like authentication, chatbot, and SDLC orchestrator functionality.
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.chatbot import router as chatbot_router
from app.api.v1.media import router as media_router
from app.api.v1.sdlc import router as sdlc_router
from app.core.config import settings
from app.core.logging import logger

api_router = APIRouter()

# Include routers
api_router.include_router(auth_router, prefix="/auth", tags=["Auth"])
api_router.include_router(chatbot_router, prefix="/chatbot", tags=["Chatbot"])
api_router.include_router(sdlc_router, prefix="/sdlc", tags=["SDLC Orchestrator"])
api_router.include_router(media_router, prefix="/media", tags=["Media Generation"])

# WebSocket router for real-time SDLC interaction (approve/reject/status).
# Opt-in via ENABLE_WEBSOCKET Feature Flag (default false). When disabled,
# the endpoint is not registered at all — SSE endpoints remain primary.
if settings.ENABLE_WEBSOCKET:
    from app.api.v1.ws import router as ws_router

    api_router.include_router(ws_router, tags=["WebSocket"])

# Knowledge Base router for document upload + RAG search.
# Opt-in via ENABLE_KNOWLEDGE_BASE Feature Flag (default false).
# When disabled, endpoints are not registered — code index remains primary.
if settings.ENABLE_KNOWLEDGE_BASE:
    from app.api.v1.knowledge_base import router as kb_router

    api_router.include_router(kb_router, prefix="/knowledge-base", tags=["Knowledge Base"])


@api_router.get("/health")
async def health_check():
    """Health check endpoint.

    Returns:
        dict: Health status information.
    """
    logger.info("health_check_called")
    return {"status": "healthy", "version": "1.0.0"}
