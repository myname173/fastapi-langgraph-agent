"""Tests for the investment-advisor system prompt content."""

from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parents[1] / "app" / "core" / "prompts" / "investment_advisor.md"


def test_advisor_prompt_covers_framework_and_disclaimer():
    """Advisor prompt covers the three-pillar framework and disclaimer."""
    assert PROMPT_PATH.exists(), "investment_advisor.md missing"
    text = PROMPT_PATH.read_text(encoding="utf-8")
    required = [
        "基本面",
        "技术面",
        "情绪面",
        "苏格拉底",
        "不构成投资建议",
        "免责",
        "风险",
    ]
    for kw in required:
        assert kw in text, f"advisor prompt missing: {kw}"
