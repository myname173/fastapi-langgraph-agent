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

When the intent is TOOL_NEEDED, the classifier also extracts the specific tool
category and stock symbol so the cloud LLM gets a structured hint — the local
1.5B does the cheap pre-processing while the cloud+tool pipeline produces the
auditable answer.  The local model NEVER generates financial content.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.config import settings
from app.core.logging import logger
from app.schemas import Message
from app.services.llm import llm_service
from app.services.llm.execution_context import set_llm_execution_context
from app.utils.graph import extract_text_content


class ChatIntent(str, Enum):
    """Intent categories for a single chat turn."""

    CASUAL = "casual"  # greetings, small talk, confirmation, daily chatter
    ANALYTICAL = "analytical"  # investment analysis / explanation without live data
    TOOL_NEEDED = "tool_needed"  # needs live market data / tools to answer


@dataclass
class IntentResult:
    """Classification result with optional tool extraction for TOOL_NEEDED."""

    intent: ChatIntent
    tool: str | None = None  # quote / fundamental / news / kline
    symbol: str | None = None  # A-share 6-digit or US ticker, empty if unknown


# ── tool vocabulary (must match registered tool names) ────────────────────
_VALID_TOOLS = frozenset({"quote", "fundamental", "news", "kline"})
_TOOL_RULES = (
    ("quote", "股价/行情/报价/价格/涨跌", "茅台现在多少钱 → quote|600519"),
    ("fundamental", "财报/估值/PE/PB/ROE/毛利率/负债率/基本面", "茅台 PE 多少 → fundamental|600519"),
    ("news", "新闻/公告/消息/利好/利空", "平安最近有什么新闻 → news|601318"),
    ("kline", "K线/技术指标/MACD/均线/走势图", "茅台 K 线 → kline|600519"),
)


_SYSTEM_PROMPT = (
    "你是金融意图分类器。根据用户消息判断它属于哪一类。\n"
    "\n"
    "输出格式（严格一行）：\n"
    "- 寒暄/闲聊/与股票投资无关 → casual\n"
    "- 投资分析/行情解读/基本面技术面讨论/解释类问题（不需要实时数据）→ analytical\n"
    "- 需要实时数据/股价/财报/新闻/技术指标 → tool_needed|{工具}|{代码}\n"
    "\n"
    "工具可选：quote(行情/股价) fundamental(财报/基本面) news(新闻/公告) kline(K线/技术指标)\n"
    "A股代码输出6位数字（如600519），美股输出大写代码（如AAPL），无法确定则留空。\n"
    "\n"
    "示例：\n"
    "你好 → casual\n"
    "帮我分析一下新能源板块 → analytical\n"
    "茅台现在多少钱 → tool_needed|quote|600519\n"
    "阿里财报怎么样 → tool_needed|fundamental|BABA\n"
    "平安最近有什么消息 → tool_needed|news|601318\n"
    "看看茅台 K 线 → tool_needed|kline|600519\n"
    "最近有什么热点新闻 → tool_needed|news|\n"
    "市盈率多少算合理 → analytical\n"
    "帮我查一下 → tool_needed||\n"
    "\n"
    "只输出标签，不要解释。"
)

_VALID_INTENTS = {i.value for i in ChatIntent}
_VALID_TOOL_TOKENS = {t.lower() for t in _VALID_TOOLS} | {""}

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


def _parse_intent_output(raw: str) -> IntentResult:
    """Parse the model's raw output into an IntentResult.

    Handles both legacy format (single word) and extended format
    (tool_needed|tool|symbol).
    """
    cleaned = raw.strip().lower()
    if not cleaned:
        return IntentResult(intent=ChatIntent.ANALYTICAL)

    parts = [p.strip() for p in cleaned.split("|")]
    label = parts[0]

    if label not in _VALID_INTENTS:
        return IntentResult(intent=ChatIntent.ANALYTICAL)

    intent = ChatIntent(label)
    if intent != ChatIntent.TOOL_NEEDED:
        return IntentResult(intent=intent)

    # Parse tool and symbol from the remaining parts.
    tool = parts[1] if len(parts) > 1 and parts[1] in _VALID_TOOL_TOKENS else None
    symbol = parts[2] if len(parts) > 2 else None

    # Guard: keep symbol short and reject obviously non-symbols.
    if symbol and (len(symbol) > 10 or not any(c.isalnum() for c in symbol)):
        symbol = None

    return IntentResult(intent=intent, tool=tool, symbol=symbol)


async def classify_chat_intent(user_message: str) -> IntentResult:
    """Classify a chat turn's intent and optionally extract tool + symbol.

    Args:
        user_message: The latest user message text.

    Returns:
        An :class:`IntentResult` with the classified intent and, for
        TOOL_NEEDED turns, the predicted tool category and stock symbol.
        Falls back to ``ANALYTICAL`` on any failure (conservative: route to
        the cloud heavy model, never break chat).
    """
    if not settings.ENABLE_CHAT_MODEL_ROUTING:
        return IntentResult(intent=ChatIntent.ANALYTICAL)

    try:
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=user_message[:500]),
        ]
        with set_llm_execution_context(
            execution_id=None,
            user_id=None,
            agent_name="chat",
            purpose="chat_intent",
        ):
            response = await llm_service.call(
                [m.model_dump() for m in messages],
                model_name=settings.LOCAL_CHAT_INTENT_MODEL,
                max_tokens=20,
                temperature=0.0,
                cacheable=False,
            )
        raw = extract_text_content(response.content).strip() if response.content else ""
        result = _parse_intent_output(raw)
        # If parsing produced a valid result, return it.
        if result.intent == ChatIntent.TOOL_NEEDED:
            logger.debug(
                "chat_intent_tool_extraction",
                intent=result.intent.value,
                tool=result.tool,
                symbol=result.symbol,
            )
            return result
        if result.intent == ChatIntent.CASUAL:
            return result
        # ANALYTICAL returned cleanly.
        if raw and raw.lower().startswith("analytical"):
            return result
        # Noisy / partial output -> rule-based fallback on the user text.
        if any(kw in user_message for kw in _ANALYTICAL_KEYWORDS):
            return IntentResult(intent=ChatIntent.TOOL_NEEDED)
        return IntentResult(intent=ChatIntent.ANALYTICAL)
    except Exception as e:  # local model down -> never break chat
        logger.warning("chat_intent_classification_failed", error=str(e))
        return IntentResult(intent=ChatIntent.ANALYTICAL)
