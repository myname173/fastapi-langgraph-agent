"""Portfolio tool for the chat stock-advisor (组合管理).

Lets the chat agent read the current user's portfolios and holdings so it can
answer questions about their own positions and P&L (e.g. "我的持仓怎么样" /
"我的组合盈亏多少").

Gated by ``ENABLE_PORTFOLIO`` in the tool registry — invisible to the LLM
when the flag is off.

The ``user_id`` is read from ``config["metadata"]["user_id"]`` (injected by
the graph), NOT from LLM-supplied arguments, so one user can never read
another user's portfolio.
"""

from typing import Any, Dict

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from app.core.config import settings
from app.core.langgraph.tools._finance_utils import ahttp_get_json, to_gtimg_symbol
from app.core.logging import logger
from app.services.portfolio_service import portfolio_service

# Cap external quote calls per invocation so a large portfolio does not turn
# one tool call into dozens of upstream requests (remaining rows fall back to
# the cost price, which is always available locally).
_MAX_LIVE_QUOTES = 10


def _is_allowed_symbol(symbol: str) -> bool:
    """Check a symbol's prefix against the quote whitelist."""
    s = symbol.strip().lower()
    return any(s.startswith(p) for p in settings.QUOTE_ALLOWED_PREFIXES)


async def _current_price(symbol: str) -> float | None:
    """Best-effort live price from the Tencent quote endpoint (None on failure)."""
    if not _is_allowed_symbol(symbol):
        return None
    try:
        raw = await ahttp_get_json("https://qt.gtimg.cn/q=" + to_gtimg_symbol(symbol))
        text = raw if isinstance(raw, str) else str(raw)
        quote = text.split("=", 1)[1].strip().rstrip(";").split("~")
        return float(quote[3]) if len(quote) > 3 else None
    except Exception as e:  # never block the answer on a price hiccup
        logger.warning("portfolio_tool_price_failed", symbol=symbol, error=str(e))
        return None


def _enum_value(v: Any) -> str:
    """Render a str-enum (or plain value) as its string form."""
    return v.value if hasattr(v, "value") else str(v)


@tool
async def get_portfolio_summary(*, config: RunnableConfig) -> Dict[str, Any]:
    """Get the current user's portfolios, holdings and profit/loss.

    Use this tool when the user asks about their own positions — e.g.
    "我的持仓怎么样" / "我的组合盈亏" / "我买了哪些股票" / "帮我看看我的组合".

    Args:
        config: LangChain runtime config (auto-injected, not LLM-visible).
            Provides ``config["metadata"]["user_id"]`` for per-user isolation.

    Returns:
        A dict with ``portfolios`` (each holding its ``holdings`` list plus
        aggregate ``total_value`` / ``total_cost`` / ``total_profit``) and the
        grand totals. ``portfolios`` is empty when the user has none. Live
        prices are best-effort: a missing quote falls back to the cost price.
    """
    user_id = config.get("metadata", {}).get("user_id")
    if not user_id:
        return {
            "portfolios": [],
            "total_value": 0.0,
            "total_cost": 0.0,
            "total_profit": 0.0,
        }

    uid = int(user_id)
    portfolios = await portfolio_service.list_portfolios(uid)

    quote_budget = _MAX_LIVE_QUOTES
    out: list[dict] = []
    grand_value = 0.0
    grand_cost = 0.0

    for p in portfolios:
        holdings = await portfolio_service.get_holdings(p.id)
        rows: list[dict] = []
        p_value = float(p.cash or 0.0)
        p_cost = float(p.cash or 0.0)

        for h in holdings:
            price: float | None = None
            if quote_budget > 0:
                price = await _current_price(h.symbol)
                quote_budget -= 1
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
                    "price": round(price, 3),
                    "value": round(value, 2),
                    "profit": round(value - cost, 2),
                }
            )
            p_value += value
            p_cost += cost

        out.append(
            {
                "name": p.name,
                "risk_tolerance": _enum_value(p.risk_tolerance),
                "cash": round(float(p.cash or 0.0), 2),
                "total_value": round(p_value, 2),
                "total_cost": round(p_cost, 2),
                "total_profit": round(p_value - p_cost, 2),
                "total_profit_pct": round((p_value - p_cost) / p_cost * 100, 2) if p_cost else 0.0,
                "holdings": rows,
            }
        )
        grand_value += p_value
        grand_cost += p_cost

    logger.info("portfolio_tool_summary", user_id=uid, portfolio_count=len(out))
    return {
        "portfolios": out,
        "total_value": round(grand_value, 2),
        "total_cost": round(grand_cost, 2),
        "total_profit": round(grand_value - grand_cost, 2),
    }


portfolio_tools_list = [get_portfolio_summary]
