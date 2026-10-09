"""Price-alert tool for the chat stock-advisor (价格预警).

Lets the chat agent read the current user's price alerts so it can answer
questions about their own reminders (e.g. "我设了哪些预警" / "我的提醒还在吗").

Gated by ``ENABLE_ALERT`` in the tool registry — invisible to the LLM when
the flag is off.

The ``user_id`` is read from ``config["metadata"]["user_id"]`` (injected by
the graph), NOT from LLM-supplied arguments, so one user can never read
another user's alerts.
"""

from typing import Any, List

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from app.core.logging import logger
from app.services.alert_service import alert_service


def _enum_value(v: Any) -> str:
    """Render a str-enum (or plain value) as its string form."""
    return v.value if hasattr(v, "value") else str(v)


@tool
async def get_price_alerts(active_only: bool = True, *, config: RunnableConfig) -> List[dict]:
    """Get the current user's price alerts (价格预警).

    Use this tool when the user asks about their own alerts / reminders — e.g.
    "我设了哪些预警" / "我的价格提醒" / "有没有已经触发的预警".

    Args:
        active_only: When True (default) return only alerts that are still
            armed. Pass False to include alerts that have already fired.
        config: LangChain runtime config (auto-injected, not LLM-visible).
            Provides ``config["metadata"]["user_id"]`` for per-user isolation.

    Returns:
        List of alerts, each with ``symbol``, ``name``, ``condition``
        ("above" = 突破上方 / "below" = 跌破下方), ``price``, ``active``,
        ``note`` and ``fired_at``. Empty list when the user has none.
    """
    user_id = config.get("metadata", {}).get("user_id")
    if not user_id:
        return []

    alerts = await alert_service.list(user_id=int(user_id), active_only=active_only)
    logger.info("alert_tool_list", user_id=user_id, alert_count=len(alerts))
    return [
        {
            "symbol": a.symbol,
            "name": a.name,
            "condition": _enum_value(a.condition),
            "price": a.price,
            "active": a.active,
            "note": a.note,
            "fired_at": a.fired_at.isoformat() if a.fired_at else None,
        }
        for a in alerts
    ]


alert_tools_list = [get_price_alerts]
