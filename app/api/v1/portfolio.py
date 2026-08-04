"""Portfolio management API for the chat stock-advisor.

CRUD for portfolios / holdings plus an aggregation endpoint (cost basis,
live market value, PnL, allocation). Auth, rate-limiting, and error handling
mirror ``app/api/v1/sdlc.py``. Registered only when ``ENABLE_PORTFOLIO`` is on
(see ``api.py``), so it is inert by default.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.v1.auth import get_current_session
from app.core.config import settings
from app.core.langgraph.tools._finance_utils import ahttp_get_json, to_gtimg_symbol
from app.core.limiter import limiter
from app.core.logging import logger
from app.models.portfolio import Portfolio, RiskTolerance
from app.models.session import Session
from app.services.portfolio_service import portfolio_service
from app.utils.errors import safe_500

router = APIRouter()


# ----------------------------------------------------------------------
# Request schemas
# ----------------------------------------------------------------------
class PortfolioCreate(BaseModel):
    """Create-portfolio payload."""

    name: str = "我的组合"
    risk_tolerance: str = "balanced"
    cash: float = 0.0


class PortfolioUpdate(BaseModel):
    """Update-portfolio payload (all optional)."""

    name: Optional[str] = None
    risk_tolerance: Optional[str] = None
    cash: Optional[float] = None


class HoldingCreate(BaseModel):
    """Add-holding payload."""

    symbol: str
    name: str = ""
    shares: float = Field(default=0.0, ge=0)
    cost_price: float = 0.0


class HoldingUpdate(BaseModel):
    """Update-holding payload (all optional)."""

    name: Optional[str] = None
    shares: Optional[float] = None
    cost_price: Optional[float] = None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
async def _require_portfolio(pid: str, user_id: int) -> Portfolio:
    """Fetch a portfolio, raising 404 if missing or not owned by the user."""
    p = await portfolio_service.get_portfolio(pid)
    if not p or p.user_id != user_id:
        raise HTTPException(status_code=404, detail="组合不存在")
    return p


def _is_allowed_symbol(symbol: str) -> bool:
    """Check if a symbol's prefix is in the whitelist."""
    s = symbol.strip().lower()
    return any(s.startswith(p) for p in settings.QUOTE_ALLOWED_PREFIXES)


async def _current_price(symbol: str) -> Optional[float]:
    """Best-effort live price from the Tencent quote endpoint."""
    if not _is_allowed_symbol(symbol):
        logger.warning("portfolio_price_symbol_rejected", symbol=symbol)
        return None
    gsym = to_gtimg_symbol(symbol)
    try:
        raw = await ahttp_get_json("https://qt.gtimg.cn/q=" + gsym)
        text = raw if isinstance(raw, str) else str(raw)
        quote = text.split("=", 1)[1].strip().rstrip(";").split("~")
        return float(quote[3]) if len(quote) > 3 else None
    except Exception as e:  # never block analysis on a price hiccup
        logger.warning("portfolio_price_failed", symbol=symbol, error=str(e))
        return None


# ----------------------------------------------------------------------
# Portfolio routes
# ----------------------------------------------------------------------
@router.post("")
@limiter.limit("10 per minute")
async def create_portfolio(
    request: Request,
    body: PortfolioCreate,
    session: Session = Depends(get_current_session),
):
    """Create a new portfolio for the current user."""
    try:
        risk = RiskTolerance(body.risk_tolerance)
    except ValueError:
        raise HTTPException(status_code=400, detail="risk_tolerance 非法")
    try:
        p = await portfolio_service.create_portfolio(session.user_id, body.name, risk, body.cash)
        return p.model_dump()
    except Exception as e:
        logger.error("portfolio_create_failed", error=str(e))
        raise safe_500(e, context="portfolio")


@router.get("")
@limiter.limit("20 per minute")
async def list_portfolios(
    request: Request,
    session: Session = Depends(get_current_session),
):
    """List the current user's portfolios."""
    try:
        items = await portfolio_service.list_portfolios(session.user_id)
        return [p.model_dump() for p in items]
    except Exception as e:
        logger.error("portfolio_list_failed", error=str(e))
        raise safe_500(e, context="portfolio")


@router.get("/{pid}")
@limiter.limit("30 per minute")
async def get_portfolio(
    pid: str,
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Get a single portfolio (must belong to the user)."""
    try:
        p = await _require_portfolio(pid, session.user_id)
        return p.model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("portfolio_get_failed", error=str(e))
        raise safe_500(e, context="portfolio")


@router.put("/{pid}")
@limiter.limit("20 per minute")
async def update_portfolio(
    pid: str,
    request: Request,
    body: PortfolioUpdate,
    session: Session = Depends(get_current_session),
):
    """Update portfolio name / risk / cash."""
    try:
        await _require_portfolio(pid, session.user_id)
        risk = RiskTolerance(body.risk_tolerance) if body.risk_tolerance else None
        p = await portfolio_service.update_portfolio(pid, name=body.name, risk=risk, cash=body.cash)
        if not p:
            raise HTTPException(status_code=404, detail="组合不存在")
        return p.model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("portfolio_update_failed", error=str(e))
        raise safe_500(e, context="portfolio")


@router.delete("/{pid}")
@limiter.limit("10 per minute")
async def delete_portfolio(
    pid: str,
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Delete a portfolio and its holdings."""
    try:
        await _require_portfolio(pid, session.user_id)
        ok = await portfolio_service.delete_portfolio(pid)
        return {"deleted": ok}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("portfolio_delete_failed", error=str(e))
        raise safe_500(e, context="portfolio")


# ----------------------------------------------------------------------
# Holding routes
# ----------------------------------------------------------------------
@router.post("/{pid}/holdings")
@limiter.limit("20 per minute")
async def add_holding(
    pid: str,
    request: Request,
    body: HoldingCreate,
    session: Session = Depends(get_current_session),
):
    """Add a holding to a portfolio."""
    try:
        await _require_portfolio(pid, session.user_id)
        if not _is_allowed_symbol(body.symbol):
            raise HTTPException(
                status_code=400,
                detail=f"股票代码前缀不在允许列表中。允许的前缀：{settings.QUOTE_ALLOWED_PREFIXES}",
            )
        h = await portfolio_service.add_holding(pid, body.symbol, body.name, body.shares, body.cost_price)
        if not h:
            raise HTTPException(status_code=404, detail="组合不存在")
        return h.model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("holding_add_failed", error=str(e))
        raise safe_500(e, context="portfolio")


@router.put("/{pid}/holdings/{hid}")
@limiter.limit("20 per minute")
async def update_holding(
    pid: str,
    hid: str,
    request: Request,
    body: HoldingUpdate,
    session: Session = Depends(get_current_session),
):
    """Update a holding."""
    try:
        await _require_portfolio(pid, session.user_id)
        h = await portfolio_service.update_holding(hid, shares=body.shares, cost_price=body.cost_price, name=body.name)
        if not h:
            raise HTTPException(status_code=404, detail="持仓不存在")
        return h.model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("holding_update_failed", error=str(e))
        raise safe_500(e, context="portfolio")


@router.delete("/{pid}/holdings/{hid}")
@limiter.limit("20 per minute")
async def delete_holding(
    pid: str,
    hid: str,
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Delete a holding."""
    try:
        await _require_portfolio(pid, session.user_id)
        ok = await portfolio_service.delete_holding(hid)
        return {"deleted": ok}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("holding_delete_failed", error=str(e))
        raise safe_500(e, context="portfolio")


# ----------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------
@router.get("/{pid}/analysis")
@limiter.limit("20 per minute")
async def analyze_portfolio(
    pid: str,
    request: Request,
    session: Session = Depends(get_current_session),
):
    """Aggregate cost basis, live market value, PnL and allocation.

    Live prices are best-effort; a missing quote falls back to the cost price
    so the aggregation still renders instead of failing.
    """
    try:
        await _require_portfolio(pid, session.user_id)
        holdings = await portfolio_service.get_holdings(pid)
        p = await portfolio_service.get_portfolio(pid)
        rows = []
        total_value = p.cash if p else 0.0
        total_cost = p.cash if p else 0.0
        for h in holdings:
            price = await _current_price(h.symbol)
            if price is None:
                price = h.cost_price
            value = price * h.shares
            cost = h.cost_price * h.shares
            rows.append(
                {
                    "symbol": h.symbol,
                    "name": h.name,
                    "shares": h.shares,
                    "cost_price": h.cost_price,
                    "price": price,
                    "value": round(value, 2),
                    "profit": round(value - cost, 2),
                    "profit_pct": round((value - cost) / cost * 100, 2) if cost else 0.0,
                }
            )
            total_value += value
            total_cost += cost
        for r in rows:
            r["weight_pct"] = round(r["value"] / total_value * 100, 2) if total_value else 0.0
        return {
            "portfolio_id": pid,
            "cash": p.cash if p else 0.0,
            "total_value": round(total_value, 2),
            "total_cost": round(total_cost, 2),
            "total_profit": round(total_value - total_cost, 2),
            "total_profit_pct": (round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0.0),
            "holdings": rows,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("portfolio_analysis_failed", error=str(e))
        raise safe_500(e, context="portfolio")
