"""FinMem-style layered memory for the chat stock-advisor.

Wraps :class:`MemoryService` so stock-specific and risk-profile memories are
tagged with structured ``metadata``. This lets the advisor recall a user's
watched symbols and risk appetite as distinct dimensions, while leaving the
existing generic long-term memory untouched.

All reads degrade gracefully: if the underlying mem0 backend does not support
the ``filters`` argument (version differences), the call falls back to a plain
semantic search.
"""

from __future__ import annotations

from typing import Optional

from app.core.config import settings
from app.core.logging import logger
from app.services.memory import memory_service

STOCK_MEMORY_TYPE = "stock_memory"
RISK_PROFILE_TYPE = "risk_profile"


async def add_stock_memory(user_id: str, text: str, symbol: Optional[str] = None) -> None:
    """Persist a stock-related fact tagged with structured metadata.

    Args:
        user_id: Owner of the memory.
        text: The fact to remember (e.g. "用户长期关注贵州茅台").
        symbol: Optional ticker (e.g. sh600519) for per-symbol recall.
    """
    metadata = {"type": STOCK_MEMORY_TYPE}
    if symbol:
        metadata["symbol"] = symbol
    await memory_service.add(user_id, [{"role": "user", "content": text}], metadata=metadata)


async def add_risk_profile(user_id: str, text: str) -> None:
    """Persist the user's risk profile / investment goals.

    Args:
        user_id: Owner of the memory.
        text: Risk-profile description (e.g. "稳健型，偏红利，长线持有").
    """
    await memory_service.add(
        user_id,
        [{"role": "user", "content": text}],
        metadata={"type": RISK_PROFILE_TYPE},
    )


async def search_stock_memory(user_id: str, query: str, symbol: Optional[str] = None) -> str:
    """Recall stock memories, optionally scoped to a single symbol.

    Args:
        user_id: Owner of the memory.
        query: Semantic query.
        symbol: Optional ticker to narrow the recall.

    Returns:
        Formatted memory string, or "" on failure / no match.
    """
    filters = {"metadata": {"type": STOCK_MEMORY_TYPE}}
    if symbol:
        filters["metadata"]["symbol"] = symbol
    try:
        return await memory_service.search(user_id, query, filters=filters)
    except Exception as e:
        logger.warning("finance_memory_search_filtered_failed", error=str(e))
        return await memory_service.search(user_id, query)


async def search_risk_profile(user_id: str, query: str = "用户风险偏好与投资目标") -> str:
    """Recall the user's risk-profile memory.

    Args:
        user_id: Owner of the memory.
        query: Semantic query (defaults to a risk-profile probe).

    Returns:
        Formatted memory string, or "" on failure / no match.
    """
    try:
        return await memory_service.search(user_id, query, filters={"metadata": {"type": RISK_PROFILE_TYPE}})
    except Exception as e:
        logger.warning("finance_memory_risk_search_failed", error=str(e))
        return await memory_service.search(user_id, query)


async def remember_symbol_if_new(user_id: str, symbol: str, name: str = "") -> None:
    """Best-effort: remember that the user is interested in ``symbol``.

    Called from the chat turn after a real-time market tool fires, so the
    advisor "remembers" what the user watches without manual prompting.

    Args:
        user_id: Owner of the memory.
        symbol: Ticker that appeared in the user's query/tool call.
        name: Optional human-readable name.
    """
    if not settings.ENABLE_FINANCIAL_MEMORY or not user_id:
        return
    label = f"{name}（{symbol}）" if name else symbol
    try:
        await add_stock_memory(user_id, f"用户关注/查询过标的：{label}", symbol=symbol)
    except Exception as e:
        logger.warning("finance_memory_remember_failed", symbol=symbol, error=str(e))
