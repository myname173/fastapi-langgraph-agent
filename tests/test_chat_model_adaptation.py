"""Tests for chat model adaptation (per-turn routing, zero-regression)."""

from app.core.chat_intent import ChatIntent, classify_chat_intent
from app.core.config import settings
from app.services.llm import llm_service


def test_flags_default_off():
    """All stock-advisor feature flags default to off (zero regression)."""
    for flag in [
        "ENABLE_INVESTMENT_ADVISOR",
        "ENABLE_FUNDAMENTALS",
        "ENABLE_KLINE",
        "ENABLE_NEWS",
        "ENABLE_PORTFOLIO",
        "ENABLE_FINANCIAL_MEMORY",
        "ENABLE_CHART_GEN",
        "ENABLE_RISK_PROFILE",
        "ENABLE_RESEARCH_DEBATE",
    ]:
        assert getattr(settings, flag) is False, flag


def test_routing_flags_default_on():
    """Chat routing / tool extraction / disclaimer flags are ON by default."""
    for flag in [
        "ENABLE_CHAT_MODEL_ROUTING",
        "ENABLE_LOCAL_CHAT_TIER",
        "ENABLE_SYMBOL_HINT",
        "ENABLE_CHAT_DISCLAIMER",
    ]:
        assert getattr(settings, flag) is True, flag


async def test_intent_fallback_on_local_failure(monkeypatch):
    """Local model failure degrades to ANALYTICAL (cloud), never breaks chat."""
    monkeypatch.setattr(settings, "ENABLE_CHAT_MODEL_ROUTING", True)
    monkeypatch.setattr(settings, "ENABLE_LOCAL_CHAT_TIER", True)

    async def boom(*a, **k):
        raise RuntimeError("local model down")

    monkeypatch.setattr(llm_service, "call", boom)
    # Failure must degrade to ANALYTICAL (cloud), never break chat.
    result = await classify_chat_intent("你好")
    assert result.intent == ChatIntent.ANALYTICAL
    assert result.tool is None
    assert result.symbol is None


async def test_intent_classifies_casual(monkeypatch):
    """A 'casual' label from the local model routes the turn locally."""
    monkeypatch.setattr(settings, "ENABLE_CHAT_MODEL_ROUTING", True)
    monkeypatch.setattr(settings, "ENABLE_LOCAL_CHAT_TIER", True)

    async def ok(*a, **k):
        class R:
            content = "casual"

        return R()

    monkeypatch.setattr(llm_service, "call", ok)
    result = await classify_chat_intent("你好呀")
    assert result.intent == ChatIntent.CASUAL
    assert result.tool is None


async def test_intent_classifies_tool_needed(monkeypatch):
    """A 'tool_needed' label is returned for live-data questions."""
    monkeypatch.setattr(settings, "ENABLE_CHAT_MODEL_ROUTING", True)
    monkeypatch.setattr(settings, "ENABLE_LOCAL_CHAT_TIER", True)

    async def ok(*a, **k):
        class R:
            content = "tool_needed"

        return R()

    monkeypatch.setattr(llm_service, "call", ok)
    result = await classify_chat_intent("茅台最新股价多少")
    assert result.intent == ChatIntent.TOOL_NEEDED
    # Bare "tool_needed" without | produces None for tool/symbol
    assert result.tool is None
    assert result.symbol is None


async def test_intent_extracts_tool_and_symbol(monkeypatch):
    """TOOL_NEEDED with |tool|symbol extracts structured fields."""
    monkeypatch.setattr(settings, "ENABLE_CHAT_MODEL_ROUTING", True)
    monkeypatch.setattr(settings, "ENABLE_LOCAL_CHAT_TIER", True)

    async def ok(*a, **k):
        class R:
            content = "tool_needed|quote|600519"

        return R()

    monkeypatch.setattr(llm_service, "call", ok)
    result = await classify_chat_intent("茅台现在多少钱")
    assert result.intent == ChatIntent.TOOL_NEEDED
    assert result.tool == "quote"
    assert result.symbol == "600519"


async def test_intent_routing_off_returns_analytical(monkeypatch):
    """With routing disabled, classification is a no-op returning ANALYTICAL."""
    monkeypatch.setattr(settings, "ENABLE_CHAT_MODEL_ROUTING", False)
    result = await classify_chat_intent("随便什么")
    assert result.intent == ChatIntent.ANALYTICAL
    assert result.tool is None
