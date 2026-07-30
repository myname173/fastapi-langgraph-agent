"""Risk-profile service for the chat stock-advisor.

Mirrors the dual-path (sync-block / native-async) pattern of SDLCTaskService.
Upsert-by-user (primary key = user_id) so setting a profile is idempotent.
"""

from __future__ import annotations

from datetime import datetime, UTC
from typing import Optional

from sqlmodel import Session, select

from app.core.config import settings
from app.core.db.session import get_async_session_maker
from app.core.logging import logger
from app.models.portfolio import RiskTolerance
from app.models.risk_profile import RiskProfile
from app.services.database import database_service


def _to_profile(user_id: int, risk: str, goals: str, horizon: str, monthly: float, experience: str) -> RiskProfile:
    try:
        rt = RiskTolerance(risk)
    except ValueError:
        rt = RiskTolerance.BALANCED
    return RiskProfile(
        user_id=user_id,
        risk_tolerance=rt,
        investment_goals=goals,
        horizon=horizon,
        monthly_invest=monthly,
        experience=experience,
    )


class RiskProfileService:
    """Service managing a user's risk profile."""

    # --- sync blocks -------------------------------------------------
    def _get_sync(self, user_id: int) -> Optional[RiskProfile]:
        def _block(session: Session) -> Optional[RiskProfile]:
            return session.exec(select(RiskProfile).where(RiskProfile.user_id == user_id)).first()

        return database_service.run_in_session(_block, op="risk_profile_get")

    def _upsert_sync(
        self, user_id: int, risk: str, goals: str, horizon: str, monthly: float, experience: str
    ) -> RiskProfile:
        def _block(session: Session) -> RiskProfile:
            existing = session.exec(select(RiskProfile).where(RiskProfile.user_id == user_id)).first()
            if existing:
                p = existing
            else:
                p = RiskProfile(user_id=user_id)
            p.risk_tolerance = _to_profile(user_id, risk, goals, horizon, monthly, experience).risk_tolerance
            p.investment_goals = goals
            p.horizon = horizon
            p.monthly_invest = monthly
            p.experience = experience
            p.updated_at = datetime.now(UTC)
            session.add(p)
            session.commit()
            session.refresh(p)
            logger.info("risk_profile_upserted", user_id=user_id)
            return p

        return database_service.run_in_session(_block, op="risk_profile_upsert")

    # --- async -------------------------------------------------------
    async def _get_async(self, user_id: int) -> Optional[RiskProfile]:
        async with get_async_session_maker()() as session:
            return (await session.execute(select(RiskProfile).where(RiskProfile.user_id == user_id))).scalars().first()

    async def _upsert_async(
        self, user_id: int, risk: str, goals: str, horizon: str, monthly: float, experience: str
    ) -> RiskProfile:
        async with get_async_session_maker()() as session:
            existing = (
                (await session.execute(select(RiskProfile).where(RiskProfile.user_id == user_id))).scalars().first()
            )
            p = existing or RiskProfile(user_id=user_id)
            p.risk_tolerance = _to_profile(user_id, risk, goals, horizon, monthly, experience).risk_tolerance
            p.investment_goals = goals
            p.horizon = horizon
            p.monthly_invest = monthly
            p.experience = experience
            p.updated_at = datetime.now(UTC)
            session.add(p)
            await session.commit()
            await session.refresh(p)
            logger.info("risk_profile_upserted", user_id=user_id)
            return p

    # --- public ------------------------------------------------------
    async def get(self, user_id: int) -> Optional[RiskProfile]:
        """Get a user's risk profile."""
        if settings.ENABLE_ASYNC_DB:
            return await self._get_async(user_id)
        return await run_in_thread_rp(self._get_sync, user_id, op="risk_profile_get")

    async def upsert(
        self,
        user_id: int,
        risk: str = "balanced",
        goals: str = "",
        horizon: str = "",
        monthly: float = 0.0,
        experience: str = "",
    ) -> RiskProfile:
        """Create or update a user's risk profile (idempotent)."""
        if settings.ENABLE_ASYNC_DB:
            return await self._upsert_async(user_id, risk, goals, horizon, monthly, experience)
        return await run_in_thread_rp(
            self._upsert_sync,
            user_id,
            risk,
            goals,
            horizon,
            monthly,
            experience,
            op="risk_profile_upsert",
        )


async def run_in_thread_rp(func, *args, op: str = "risk_profile", **kwargs):
    """Dispatch a sync DB block off the event loop (mirrors SDLCTaskService)."""
    from app.core.db_executor import run_in_thread

    return await run_in_thread(func, *args, op=op, **kwargs)


risk_profile_service = RiskProfileService()
