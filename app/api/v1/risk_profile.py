"""Risk-profile API for the chat stock-advisor.

Lets the user set / read their investment risk profile. Auth, rate-limiting
and error handling mirror ``sdlc.py``. Registered only when
``ENABLE_RISK_PROFILE`` is on.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.v1.auth import get_current_session
from app.core.limiter import limiter
from app.core.logging import logger
from app.models.session import Session
from app.services.risk_profile_service import risk_profile_service
from app.utils.errors import safe_500

router = APIRouter()


class RiskProfileUpdate(BaseModel):
    """Risk-profile payload (all optional on update)."""

    risk_tolerance: Optional[str] = None
    investment_goals: Optional[str] = None
    horizon: Optional[str] = None
    monthly_invest: Optional[float] = None
    experience: Optional[str] = None


@router.get("/me")
@limiter.limit("20 per minute")
async def get_my_profile(
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Get the current user's risk profile."""
    try:
        p = await risk_profile_service.get(session.user_id)
        if not p:
            return {"risk_profile": None}
        return {"risk_profile": p.model_dump()}
    except Exception as e:
        logger.error("risk_profile_get_failed", error=str(e))
        return safe_500(e)


@router.put("/me")
@limiter.limit("10 per minute")
async def upsert_my_profile(
    request: Request,
    body: RiskProfileUpdate,
    session: Session = Depends(get_current_session),
):
    """Create or update the current user's risk profile."""
    try:
        p = await risk_profile_service.upsert(
            session.user_id,
            risk=body.risk_tolerance or "balanced",
            goals=body.investment_goals or "",
            horizon=body.horizon or "",
            monthly=body.monthly_invest or 0.0,
            experience=body.experience or "",
        )
        return p.model_dump()
    except Exception as e:
        logger.error("risk_profile_upsert_failed", error=str(e))
        return safe_500(e)
