"""Regex-based A-share symbol extraction and chat disclaimer gating.

The chat agent binds tools (e.g. ``fetch_realtime_quote``) to the cloud LLM,
which produces ticker codes itself as tool-call args. That extraction is
bundled into the cloud inference — there is no separate call to save. This
module instead provides a *deterministic, zero-LLM-cost* pre-extraction of
A-share codes from the user's text, used for two purposes:

1. **Non-binding hint**: extracted codes are injected as a system message so
   the cloud LLM can use them as tool-arg reference (it may ignore them).
   This is safer than asking a 1.5B model to do NER (which can hallucinate
   valid-format-but-wrong tickers), because regex only matches the code
   *format* and every result is filtered through
   ``settings.QUOTE_ALLOWED_PREFIXES`` — the same whitelist the tool enforces.
2. **Disclaimer gating**: ``needs_disclaimer()`` decides whether a hardcoded
   compliance disclaimer is appended to the answer (symbol hit or financial
   keyword in the user turn).

All callers gate on their respective ``settings.ENABLE_*`` flags (default
off), so this module is a no-op when nothing is enabled.
"""

from __future__ import annotations

import re
from typing import List

from app.core.config import settings

# Prefixed tickers: sh600519 / sz000858 / hk00700 / usAAPL / hf_GC.
# Digits cover A-share (6) and HK (4-5); letters cover US tickers and
# commodities. Whitespace tolerated between prefix and code.
# NOTE: boundaries are ASCII-only lookarounds, NOT ``\b``. Python's ``\b``
# treats CJK characters as word chars, so ``\b`` between a Chinese char and
# a digit does not match — which would break the primary use case
# ("帮我查一下600519的行情"). ``(?<![0-9A-Za-z])`` lets Chinese chars act as
# boundaries while still preventing matches inside longer ASCII identifiers
# (e.g. ``x600519``, ``6005190``).
_PREFIXED_RE = re.compile(
    r"(?<![0-9A-Za-z])(sh|sz|hk|us|hf_)\s*(\d{4,6}|[A-Za-z]{1,5})(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# Bare A-share 6-digit codes, mapped to their exchange prefix:
#   60xxxx / 688xxx -> sh (Shanghai main / STAR)
#   00xxxx / 30xxxx -> sz (Shenzhen main / ChiNext)
# 688 listed first so the longer-specific prefix wins at the same position.
# Same ASCII-only boundary rationale as ``_PREFIXED_RE``.
_BARE_RE = re.compile(r"(?<![0-9A-Za-z])(688\d{3}|60\d{4}|00\d{4}|30\d{4})(?![0-9A-Za-z])")

# Financial keywords reused from chat_intent._ANALYTICAL_KEYWORDS (copied to
# avoid importing a private). Drives needs_disclaimer() for turns that mention
# stocks by name rather than code.
FINANCIAL_KEYWORDS = (
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


def _normalize_prefixed(prefix: str, code: str) -> str:
    """Normalize a prefixed ticker to the tool-expected form.

    Lowercases the market prefix (sh/sz/hk/us/hf_) and uppercases letter
    codes (US tickers / commodities); digit codes are kept as-is.

    Args:
        prefix: The matched market prefix (e.g. "sh", "US").
        code: The matched code (e.g. "600519", "aapl", "GC").

    Returns:
        The normalized symbol, e.g. "sh600519", "usAAPL", "hf_GC".
    """
    return f"{prefix.lower()}{code.upper()}"


def _bare_to_prefixed(code: str) -> str:
    """Add the correct exchange prefix to a bare A-share 6-digit code.

    Args:
        code: A bare 6-digit A-share code (e.g. "600519", "300750").

    Returns:
        The prefixed symbol, e.g. "sh600519", "sz300750".
    """
    if code.startswith("688") or code.startswith("60"):
        return f"sh{code}"
    return f"sz{code}"  # 00xxxx / 30xxxx


def extract_a_share_symbols(text: str) -> List[str]:
    """Extract ticker symbols from user text via deterministic regex.

    Matches both explicitly prefixed tickers (``sh600519``, ``usAAPL``) and
    bare A-share 6-digit codes (``600519`` -> ``sh600519``). Results are
    de-duplicated (preserving first-seen order) and filtered through
    ``settings.QUOTE_ALLOWED_PREFIXES`` so a hint never suggests a symbol the
    tool would reject. Capped at ``settings.QUOTE_MAX_SYMBOLS_PER_CALL``.

    Args:
        text: The raw user message text.

    Returns:
        Normalized symbols ready for tool args (e.g. ``["sh600519",
        "sz000858"]``). Empty list when nothing matched.
    """
    if not text:
        return []

    found: list[str] = []

    for m in _PREFIXED_RE.finditer(text):
        found.append(_normalize_prefixed(m.group(1), m.group(2)))

    for m in _BARE_RE.finditer(text):
        found.append(_bare_to_prefixed(m.group(1)))

    if not found:
        return []

    # De-duplicate preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for sym in found:
        if sym not in seen:
            seen.add(sym)
            deduped.append(sym)

    # Whitelist filter — same check the tool enforces (quote_tools.py:183).
    allowed = settings.QUOTE_ALLOWED_PREFIXES
    filtered = [s for s in deduped if any(s.startswith(p) for p in allowed)]

    # Per-call cap.
    return filtered[: settings.QUOTE_MAX_SYMBOLS_PER_CALL]


def needs_disclaimer(user_text: str) -> bool:
    """Decide whether a hardcoded disclaimer should follow the answer.

    True when the user turn mentions a ticker code (regex hit) or any
    financial keyword (行情/股价/分析/持仓/…). CASUAL chit-chat without such
    signals returns False, so greetings stay clean.

    Args:
        user_text: The latest user message text.

    Returns:
        True if a disclaimer should be appended.
    """
    if not user_text:
        return False
    if extract_a_share_symbols(user_text):
        return True
    return any(kw in user_text for kw in FINANCIAL_KEYWORDS)


# Hardcoded compliance disclaimer, overridable via CHAT_DISCLAIMER_TEXT.
DISCLAIMER_TEXT = settings.CHAT_DISCLAIMER_TEXT
