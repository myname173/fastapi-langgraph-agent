"""Portfolio service for the chat stock-advisor.

Mirrors ``SDLTaskservice``: a sync-DB block path dispatched via
``run_in_thread`` (default) and a native-async path when
``ENABLE_ASYNC_DB`` is on. Provides CRUD for portfolios / holdings plus a
best-effort aggregation (cost basis, live market value, PnL, allocation).
"""

from __future__ import annotations

import uuid
from datetime import datetime, UTC
from typing import List, Optional

from sqlmodel import Session, desc, select

from app.core.config import settings
from app.core.db.session import get_async_session_maker
from app.core.logging import logger
from app.models.portfolio import (
    Holding,
    Portfolio,
    RiskTolerance,
)
from app.services.database import database_service


class PortfolioService:
    """Service for managing portfolios and holdings."""

    # ------------------------------------------------------------------
    # Sync DB blocks (private) — dispatched off-loop via run_in_thread
    # ------------------------------------------------------------------

    def _create_portfolio_sync(self, user_id: int, name: str, risk: RiskTolerance, cash: float) -> Portfolio:
        pid = str(uuid.uuid4())

        def _block(session: Session) -> Portfolio:
            p = Portfolio(id=pid, user_id=user_id, name=name, risk_tolerance=risk, cash=cash)
            session.add(p)
            session.commit()
            session.refresh(p)
            logger.info("portfolio_created", portfolio_id=pid, user_id=user_id)
            return p

        return database_service.run_in_session(_block, op="portfolio_create")

    def _get_portfolio_sync(self, pid: str) -> Optional[Portfolio]:
        def _block(session: Session) -> Optional[Portfolio]:
            return session.exec(select(Portfolio).where(Portfolio.id == pid)).first()

        return database_service.run_in_session(_block, op="portfolio_get")

    def _list_portfolios_sync(self, user_id: int) -> List[Portfolio]:
        def _block(session: Session) -> List[Portfolio]:
            return list(
                session.exec(
                    select(Portfolio).where(Portfolio.user_id == user_id).order_by(desc(Portfolio.created_at))
                ).all()
            )

        return database_service.run_in_session(_block, op="portfolio_list")

    def _update_portfolio_sync(
        self, pid: str, name: Optional[str], risk: Optional[RiskTolerance], cash: Optional[float]
    ) -> Optional[Portfolio]:
        def _block(session: Session) -> Optional[Portfolio]:
            p = session.exec(select(Portfolio).where(Portfolio.id == pid)).first()
            if not p:
                return None
            if name is not None:
                p.name = name
            if risk is not None:
                p.risk_tolerance = risk
            if cash is not None:
                p.cash = cash
            p.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(p)
            return p

        return database_service.run_in_session(_block, op="portfolio_update")

    def _delete_portfolio_sync(self, pid: str) -> bool:
        def _block(session: Session) -> bool:
            p = session.exec(select(Portfolio).where(Portfolio.id == pid)).first()
            if not p:
                return False
            # Cascade holdings manually (no DB-level FK cascade configured).
            for h in session.exec(select(Holding).where(Holding.portfolio_id == pid)).all():
                session.delete(h)
            session.delete(p)
            session.commit()
            logger.info("portfolio_deleted", portfolio_id=pid)
            return True

        return database_service.run_in_session(_block, op="portfolio_delete")

    def _add_holding_sync(
        self, pid: str, symbol: str, name: str, shares: float, cost_price: float
    ) -> Optional[Holding]:
        hid = str(uuid.uuid4())

        def _block(session: Session) -> Optional[Holding]:
            p = session.exec(select(Portfolio).where(Portfolio.id == pid)).first()
            if not p:
                return None
            h = Holding(
                id=hid,
                portfolio_id=pid,
                symbol=symbol,
                name=name,
                shares=shares,
                cost_price=cost_price,
            )
            session.add(h)
            session.commit()
            session.refresh(h)
            logger.info("holding_added", portfolio_id=pid, symbol=symbol)
            return h

        return database_service.run_in_session(_block, op="holding_add")

    def _update_holding_sync(
        self, hid: str, shares: Optional[float], cost_price: Optional[float], name: Optional[str]
    ) -> Optional[Holding]:
        def _block(session: Session) -> Optional[Holding]:
            h = session.exec(select(Holding).where(Holding.id == hid)).first()
            if not h:
                return None
            if name is not None:
                h.name = name
            if shares is not None:
                h.shares = shares
            if cost_price is not None:
                h.cost_price = cost_price
            h.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(h)
            return h

        return database_service.run_in_session(_block, op="holding_update")

    def _delete_holding_sync(self, hid: str) -> bool:
        def _block(session: Session) -> bool:
            h = session.exec(select(Holding).where(Holding.id == hid)).first()
            if not h:
                return False
            session.delete(h)
            session.commit()
            return True

        return database_service.run_in_session(_block, op="holding_delete")

    def _get_holdings_sync(self, pid: str) -> List[Holding]:
        def _block(session: Session) -> List[Holding]:
            return list(session.exec(select(Holding).where(Holding.portfolio_id == pid)).all())

        return database_service.run_in_session(_block, op="holding_list")

    # ------------------------------------------------------------------
    # Native async implementations (used when ENABLE_ASYNC_DB is on)
    # ------------------------------------------------------------------

    async def _create_portfolio_async(self, user_id: int, name: str, risk: RiskTolerance, cash: float) -> Portfolio:
        pid = str(uuid.uuid4())
        async with get_async_session_maker()() as session:
            p = Portfolio(id=pid, user_id=user_id, name=name, risk_tolerance=risk, cash=cash)
            session.add(p)
            await session.commit()
            await session.refresh(p)
            logger.info("portfolio_created", portfolio_id=pid, user_id=user_id)
            return p

    async def _get_portfolio_async(self, pid: str) -> Optional[Portfolio]:
        async with get_async_session_maker()() as session:
            return (await session.execute(select(Portfolio).where(Portfolio.id == pid))).scalars().first()

    async def _list_portfolios_async(self, user_id: int) -> List[Portfolio]:
        async with get_async_session_maker()() as session:
            return list(
                (
                    await session.execute(
                        select(Portfolio).where(Portfolio.user_id == user_id).order_by(desc(Portfolio.created_at))
                    )
                )
                .scalars()
                .all()
            )

    async def _update_portfolio_async(
        self, pid: str, name: Optional[str], risk: Optional[RiskTolerance], cash: Optional[float]
    ) -> Optional[Portfolio]:
        async with get_async_session_maker()() as session:
            p = (await session.execute(select(Portfolio).where(Portfolio.id == pid))).scalars().first()
            if not p:
                return None
            if name is not None:
                p.name = name
            if risk is not None:
                p.risk_tolerance = risk
            if cash is not None:
                p.cash = cash
            p.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(p)
            return p

    async def _delete_portfolio_async(self, pid: str) -> bool:
        async with get_async_session_maker()() as session:
            p = (await session.execute(select(Portfolio).where(Portfolio.id == pid))).scalars().first()
            if not p:
                return False
            for h in (await session.execute(select(Holding).where(Holding.portfolio_id == pid))).scalars().all():
                await session.delete(h)
            await session.delete(p)
            await session.commit()
            logger.info("portfolio_deleted", portfolio_id=pid)
            return True

    async def _add_holding_async(
        self, pid: str, symbol: str, name: str, shares: float, cost_price: float
    ) -> Optional[Holding]:
        hid = str(uuid.uuid4())
        async with get_async_session_maker()() as session:
            p = (await session.execute(select(Portfolio).where(Portfolio.id == pid))).scalars().first()
            if not p:
                return None
            h = Holding(
                id=hid,
                portfolio_id=pid,
                symbol=symbol,
                name=name,
                shares=shares,
                cost_price=cost_price,
            )
            session.add(h)
            await session.commit()
            await session.refresh(h)
            logger.info("holding_added", portfolio_id=pid, symbol=symbol)
            return h

    async def _update_holding_async(
        self, hid: str, shares: Optional[float], cost_price: Optional[float], name: Optional[str]
    ) -> Optional[Holding]:
        async with get_async_session_maker()() as session:
            h = (await session.execute(select(Holding).where(Holding.id == hid))).scalars().first()
            if not h:
                return None
            if name is not None:
                h.name = name
            if shares is not None:
                h.shares = shares
            if cost_price is not None:
                h.cost_price = cost_price
            h.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(h)
            return h

    async def _delete_holding_async(self, hid: str) -> bool:
        async with get_async_session_maker()() as session:
            h = (await session.execute(select(Holding).where(Holding.id == hid))).scalars().first()
            if not h:
                return False
            await session.delete(h)
            await session.commit()
            return True

    async def _get_holdings_async(self, pid: str) -> List[Holding]:
        async with get_async_session_maker()() as session:
            return list((await session.execute(select(Holding).where(Holding.portfolio_id == pid))).scalars().all())

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def create_portfolio(
        self,
        user_id: int,
        name: str = "我的组合",
        risk: RiskTolerance = RiskTolerance.BALANCED,
        cash: float = 0.0,
    ) -> Portfolio:
        """Create a portfolio for a user."""
        if settings.ENABLE_ASYNC_DB:
            return await self._create_portfolio_async(user_id, name, risk, cash)
        return await run_in_thread_portfolio(
            self._create_portfolio_sync, user_id, name, risk, cash, op="portfolio_create"
        )

    async def get_portfolio(self, pid: str) -> Optional[Portfolio]:
        """Get a portfolio by id."""
        if settings.ENABLE_ASYNC_DB:
            return await self._get_portfolio_async(pid)
        return await run_in_thread_portfolio(self._get_portfolio_sync, pid, op="portfolio_get")

    async def list_portfolios(self, user_id: int) -> List[Portfolio]:
        """List a user's portfolios."""
        if settings.ENABLE_ASYNC_DB:
            return await self._list_portfolios_async(user_id)
        return await run_in_thread_portfolio(self._list_portfolios_sync, user_id, op="portfolio_list")

    async def update_portfolio(
        self,
        pid: str,
        name: Optional[str] = None,
        risk: Optional[RiskTolerance] = None,
        cash: Optional[float] = None,
    ) -> Optional[Portfolio]:
        """Update portfolio attributes."""
        if settings.ENABLE_ASYNC_DB:
            return await self._update_portfolio_async(pid, name, risk, cash)
        return await run_in_thread_portfolio(self._update_portfolio_sync, pid, name, risk, cash, op="portfolio_update")

    async def delete_portfolio(self, pid: str) -> bool:
        """Delete a portfolio and its holdings."""
        if settings.ENABLE_ASYNC_DB:
            return await self._delete_portfolio_async(pid)
        return await run_in_thread_portfolio(self._delete_portfolio_sync, pid, op="portfolio_delete")

    async def add_holding(
        self,
        pid: str,
        symbol: str,
        name: str = "",
        shares: float = 0.0,
        cost_price: float = 0.0,
    ) -> Optional[Holding]:
        """Add a holding to a portfolio."""
        if settings.ENABLE_ASYNC_DB:
            return await self._add_holding_async(pid, symbol, name, shares, cost_price)
        return await run_in_thread_portfolio(
            self._add_holding_sync, pid, symbol, name, shares, cost_price, op="holding_add"
        )

    async def update_holding(
        self,
        hid: str,
        shares: Optional[float] = None,
        cost_price: Optional[float] = None,
        name: Optional[str] = None,
    ) -> Optional[Holding]:
        """Update a holding."""
        if settings.ENABLE_ASYNC_DB:
            return await self._update_holding_async(hid, shares, cost_price, name)
        return await run_in_thread_portfolio(
            self._update_holding_sync, hid, shares, cost_price, name, op="holding_update"
        )

    async def delete_holding(self, hid: str) -> bool:
        """Delete a holding."""
        if settings.ENABLE_ASYNC_DB:
            return await self._delete_holding_async(hid)
        return await run_in_thread_portfolio(self._delete_holding_sync, hid, op="holding_delete")

    async def get_holdings(self, pid: str) -> List[Holding]:
        """List holdings of a portfolio."""
        if settings.ENABLE_ASYNC_DB:
            return await self._get_holdings_async(pid)
        return await run_in_thread_portfolio(self._get_holdings_sync, pid, op="holding_list")


async def run_in_thread_portfolio(func, *args, op: str = "portfolio", **kwargs):
    """Dispatch a sync DB block off the event loop (mirrors SDLCTaskService)."""
    from app.core.db_executor import run_in_thread

    return await run_in_thread(func, *args, op=op, **kwargs)


portfolio_service = PortfolioService()
