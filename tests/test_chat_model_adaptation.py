"""Tests for chat model adaptation (per-turn routing, zero-regression)."""

from app.core.chat_intent import ChatIntent, classify_chat_intent
from app.core.config import settings
from app.services.llm import llm_service


def test_flags_default_off():
    """All stock-advisor feature flags default to off (zero regression)."""
    for flag in [
        "ENABLE_CHAT_MODEL_ROUTING",
        "ENABLE_LOCAL_CHAT_TIER",
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


async def test_intent_fallback_on_local_failure(monkeypatch):
    """Local model failure degrades to ANALYTICAL (cloud), never breaks chat."""
    monkeypatch.setattr(settings, "ENABLE_CHAT_MODEL_ROUTING", True)
    monkeypatch.setattr(settings, "ENABLE_LOCAL_CHAT_TIER", True)

    async def boom(*a, **k):
        raise RuntimeError("local model down")

    monkeypatch.setattr(llm_service, "call", boom)
    # Failure must degrade to ANALYTICAL (cloud), never break chat.
    intent = await classify_chat_intent("你好")
    assert intent == ChatIntent.ANALYTICAL


async def test_intent_classifies_casual(monkeypatch):
    """A 'casual' label from the local model routes the turn locally."""
    monkeypatch.setattr(settings, "ENABLE_CHAT_MODEL_ROUTING", True)
    monkeypatch.setattr(settings, "ENABLE_LOCAL_CHAT_TIER", True)

    async def ok(*a, **k):
        class R:
            content = "casual"

        return R()

    monkeypatch.setattr(llm_service, "call", ok)
    assert await classify_chat_intent("你好呀") == ChatIntent.CASUAL


async def test_intent_classifies_tool_needed(monkeypatch):
    """A 'tool_needed' label is returned for live-data questions."""
    monkeypatch.setattr(settings, "ENABLE_CHAT_MODEL_ROUTING", True)
    monkeypatch.setattr(settings, "ENABLE_LOCAL_CHAT_TIER", True)

    async def ok(*a, **k):
        class R:
            content = "tool_needed"

        return R()

    monkeypatch.setattr(llm_service, "call", ok)
    assert await classify_chat_intent("茅台最新股价多少") == ChatIntent.TOOL_NEEDED


async def test_intent_routing_off_returns_analytical(monkeypatch):
    """With routing disabled, classification is a no-op returning ANALYTICAL."""
    monkeypatch.setattr(settings, "ENABLE_CHAT_MODEL_ROUTING", False)
    intent = await classify_chat_intent("随便什么")
    assert intent == ChatIntent.ANALYTICAL
