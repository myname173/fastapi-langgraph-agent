"""API v1 router configuration.

This module sets up the main API router and includes all sub-routers for different
endpoints like authentication, chatbot, and SDLC orchestrator functionality.
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.chatbot import router as chatbot_router
from app.api.v1.goals import router as goals_router
from app.api.v1.sdlc import router as sdlc_router
from app.core.config import settings
from app.core.logging import logger

api_router = APIRouter()

# Include routers
api_router.include_router(auth_router, prefix="/auth", tags=["Auth"])
api_router.include_router(chatbot_router, prefix="/chatbot", tags=["Chatbot"])
api_router.include_router(sdlc_router, prefix="/sdlc", tags=["SDLC Orchestrator"])
api_router.include_router(goals_router, prefix="/goals", tags=["Goals"])

# Media generation (text-to-image / text-to-video via Bailian wan2.x).
# Opt-in via ENABLE_MEDIA_GENERATION Feature Flag (default false). When
# disabled, the endpoints are not registered at all — so the
# wan2.6-t2i / wan2.7-t2v models are never called. The coder agent's
# generate_image / generate_video tools are gated by the same flag in
# app/core/langgraph/tools/registry.py.
if settings.ENABLE_MEDIA_GENERATION:
    from app.api.v1.media import router as media_router

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

# Portfolio management for the chat stock-advisor.
# Opt-in via ENABLE_PORTFOLIO Feature Flag (default false). When disabled,
# endpoints are not registered.
if settings.ENABLE_PORTFOLIO:
    from app.api.v1.portfolio import router as portfolio_router

    api_router.include_router(portfolio_router, prefix="/portfolio", tags=["Portfolio"])

# Risk-profile for the chat stock-advisor (P2 personalisation).
if settings.ENABLE_RISK_PROFILE:
    from app.api.v1.risk_profile import router as risk_profile_router

    api_router.include_router(risk_profile_router, prefix="/risk-profile", tags=["Risk Profile"])

# P2 watchlist (自选股) for the chat stock-advisor.
if settings.ENABLE_WATCHLIST:
    from app.api.v1.watchlist import router as watchlist_router

    api_router.include_router(watchlist_router, prefix="/watchlist", tags=["Watchlist"])

# P2 alert (价格预警) for the chat stock-advisor.
if settings.ENABLE_ALERT:
    from app.api.v1.alert import router as alert_router

    api_router.include_router(alert_router, prefix="/alerts", tags=["Alerts"])


@api_router.get("/health")
async def health_check():
    """Health check endpoint.

    Returns:
        dict: Health status information.
    """
    logger.info("health_check_called")
    return {"status": "healthy", "version": "1.0.0"}
