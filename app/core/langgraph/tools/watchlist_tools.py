"""Watchlist tool for the chat stock-advisor (自选股).

Lets the chat agent read the current user's watchlist so personalised
answers can factor in the symbols the user actually tracks
("对话时可结合自选标的").

Gated by ``ENABLE_WATCHLIST`` in the tool registry — when the flag is off
the tool is invisible to the LLM.

The ``user_id`` is read from ``config["metadata"]["user_id"]`` (injected by
the graph), NOT from LLM-supplied arguments, so one user can never read
another user's watchlist.
"""

from typing import List

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from app.core.logging import logger
from app.services.watchlist_service import watchlist_service


@tool
async def get_watchlist(*, config: RunnableConfig) -> List[dict]:
    """Get the current user's watchlist (自选股).

    Use this tool when the user refers to "我的自选" / "自选股" /
    "我关注的股票", or whenever knowing which symbols the user is tracking
    would make the answer more relevant (e.g. a market overview, or a
    follow-up like "我这些股票怎么样").

    Args:
        config: LangChain runtime config (auto-injected, not LLM-visible).
            Provides ``config["metadata"]["user_id"]`` for per-user isolation.

    Returns:
        List of watchlist items, newest first, each with ``symbol``,
        ``name`` and ``notes``. Empty list when the user has no watchlist.
    """
    user_id = config.get("metadata", {}).get("user_id")
    if not user_id:
        return []

    items = await watchlist_service.list(user_id=int(user_id))
    logger.info("watchlist_tool_list", user_id=user_id, item_count=len(items))
    return [{"symbol": i.symbol, "name": i.name, "notes": i.notes} for i in items]


watchlist_tools_list = [get_watchlist]
