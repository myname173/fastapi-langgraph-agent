"""Chat intent classification for per-turn model routing.

The chat agent serves a wide range of turns: greetings, small talk, deep
investment analysis, and tool-backed market-data questions. Routing every turn
to the same cloud heavy model wastes money and latency on trivial chit-chat.

This module classifies an incoming user message into one of three intents so
that :func:`_chat` can route casual turns to the cheap local 1.5B model and
analytical / tool-needing turns to the cloud heavy model. Classification uses
the local vLLM model (a single short-label call) and degrades safely to
``ANALYTICAL`` (the conservative cloud path) whenever the local model is
unavailable.
"""

from __future__ import annotations

from enum import Enum

from app.core.config import settings
from app.core.logging import logger
from app.schemas import Message
from app.services.llm import llm_service
from app.utils.graph import extract_text_content


class ChatIntent(str, Enum):
    """Intent categories for a single chat turn."""

    CASUAL = "casual"  # greetings, small talk, confirmation, daily chatter
    ANALYTICAL = "analytical"  # investment analysis / explanation without live data
    TOOL_NEEDED = "tool_needed"  # needs live market data / tools to answer


_SYSTEM_PROMPT = (
    "你是意图分类器。根据用户消息判断它属于哪一类，只输出一个英文单词：\n"
    "- casual：寒暄、闲聊、打招呼、确认、与股票投资无关的对话\n"
    "- analytical：投资分析、行情解读、基本面/技术面讨论、需要深入思考的解释类问题\n"
    "- tool_needed：需要查询实时行情、股价、财报、新闻等工具才能回答的问题\n"
    "只输出 casual / analytical / tool_needed 之一，不要任何解释。"
)

_VALID_INTENTS = {i.value for i in ChatIntent}

# Keyword hints used as a fallback when the local model's output is noisy.
_ANALYTICAL_KEYWORDS = (
    "行情",
    "股价",
    "报价",
    "市盈率",
    "市净率",
    "财报",
    "K线",
    "k线",
    "新闻",
    "分析",
    "估值",
    "基本面",
    "技术面",
    "买卖",
    "持仓",
    "选股",
    "分红",
)


async def classify_chat_intent(user_message: str) -> ChatIntent:
    """Classify a chat turn's intent.

    Args:
        user_message: The latest user message text.

    Returns:
        The classified :class:`ChatIntent`. Falls back to ``ANALYTICAL`` on any
        failure (conservative: route to the cloud heavy model, never break chat).
    """
    if not settings.ENABLE_CHAT_MODEL_ROUTING:
        return ChatIntent.ANALYTICAL

    # Classification only matters when we can actually do something with it
    # (route casual turns locally, or enable reasoning for analytical turns).
    if not settings.ENABLE_LOCAL_CHAT_TIER and not settings.CHAT_ENABLED_REASONING:
        return ChatIntent.ANALYTICAL

    try:
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=user_message[:500]),
        ]
        response = await llm_service.call(
            [m.model_dump() for m in messages],
            model_name=settings.LOCAL_CHAT_INTENT_MODEL,
            max_tokens=8,
            temperature=0.0,
            cacheable=False,
        )
        label = extract_text_content(response.content).strip().lower().split()[0] if response.content else ""
        if label in _VALID_INTENTS:
            return ChatIntent(label)
        # Noisy / partial output -> rule-based fallback on the user text.
        if any(kw in user_message for kw in _ANALYTICAL_KEYWORDS):
            return ChatIntent.TOOL_NEEDED
        return ChatIntent.ANALYTICAL
    except Exception as e:  # local model down -> never break chat
        logger.warning("chat_intent_classification_failed", error=str(e))
        return ChatIntent.ANALYTICAL
