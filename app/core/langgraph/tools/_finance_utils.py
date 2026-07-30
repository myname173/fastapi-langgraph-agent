"""Internal helpers for the finance tools.

Shared utilities for the chat agent's stock-advisor tools: HTTP JSON fetch,
symbol normalisation (Tencent gtimg / Eastmoney secid), lexicon-based
sentiment scoring, and pure-Python technical indicators (MA/RSI/MACD/BOLL).

These helpers contain no agent logic and no new third-party dependencies —
they rely only on the standard library so the tools stay lightweight.
"""

from __future__ import annotations

import asyncio
import json
import math
import urllib.request
from typing import Any, Optional

DEFAULT_TIMEOUT = 10

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def _sync_get_json(url: str, timeout: int) -> Any:
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def ahttp_get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> Any:
    """Fetch a URL and parse JSON in a worker thread.

    Args:
        url: Target URL.
        timeout: Socket timeout in seconds.

    Returns:
        Parsed JSON (dict or list).

    Raises:
        Exception: re-raised from the synchronous HTTP call. Callers should
        catch and degrade gracefully.
    """
    return await asyncio.to_thread(_sync_get_json, url, timeout)


def to_gtimg_symbol(symbol: str) -> str:
    """Normalise a stock symbol to Tencent (gtimg) format, e.g. ``sh600519``."""
    s = symbol.strip().lower()
    if s.startswith(("sh", "sz", "bj")):
        return s
    if s.startswith(("6", "9", "5", "1")):  # SH / SH funds & bonds
        return "sh" + s
    if s.startswith(("0", "3", "2", "4", "8")):  # SZ / ChiNext / BSE
        return "sz" + s
    return "sh" + s


def to_eastmoney_secid(symbol: str) -> str:
    """Normalise a stock symbol to an Eastmoney ``secid`` e.g. ``1.600519``."""
    s = symbol.strip().lower()
    if s.startswith("sh"):
        return "1." + s[2:]
    if s.startswith("sz"):
        return "0." + s[2:]
    if s.startswith("bj"):
        return "0." + s[2:]
    if s.startswith(("6", "9")):
        return "1." + s
    if s.startswith(("0", "3")):
        return "0." + s
    return "1." + s


_POSITIVE = [
    "涨",
    "利好",
    "增长",
    "盈利",
    "突破",
    "上调",
    "回购",
    "中标",
    "新高",
    "超预期",
    "扭亏",
    "增持",
    "扩产",
    "签单",
]
_NEGATIVE = [
    "跌",
    "利空",
    "亏损",
    "下跌",
    "风险",
    "下调",
    "减持",
    "下滑",
    "新低",
    "低于预期",
    "退市",
    "警示",
    "处罚",
    "诉讼",
    "商誉减值",
]


def score_sentiment(text: str) -> int:
    """Lexicon sentiment score in [0, 100] (50 = neutral).

    Args:
        text: News title / summary text.

    Returns:
        Sentiment score; >50 positive, <50 negative, 50 neutral.
    """
    if not text:
        return 50
    pos = sum(text.count(w) for w in _POSITIVE)
    neg = sum(text.count(w) for w in _NEGATIVE)
    total = pos + neg
    if total == 0:
        return 50
    score = 50 + 40 * (pos - neg) / total  # scale to [10, 90]
    return max(10, min(90, round(score)))


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def compute_technical_indicators(closes: list[float]) -> dict:
    """Compute MA / RSI / MACD / BOLL from an ordered close-price series.

    Args:
        closes: Close prices, oldest first.

    Returns:
        Dict with the latest indicator values (None when not enough data).
    """
    result: dict[str, Optional[float]] = {
        "ma5": None,
        "ma10": None,
        "ma20": None,
        "rsi14": None,
        "macd": None,
        "macd_signal": None,
        "macd_hist": None,
        "boll_mid": None,
        "boll_upper": None,
        "boll_lower": None,
    }
    n = len(closes)
    if n == 0:
        return result

    def ma(p: int) -> Optional[float]:
        return round(sum(closes[-p:]) / p, 3) if n >= p else None

    result["ma5"] = ma(5)
    result["ma10"] = ma(10)
    result["ma20"] = ma(20)

    if n >= 15:
        gains, losses = [], []
        for i in range(1, n):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))
        avg_g = sum(gains[-14:]) / 14
        avg_l = sum(losses[-14:]) / 14
        result["rsi14"] = 100.0 if avg_l == 0 else round(100 - 100 / (1 + avg_g / avg_l), 2)

    if n >= 26:
        diff = [a - b for a, b in zip(_ema(closes, 12)[-n + 25 :], _ema(closes, 26)[-n + 25 :], strict=False)]
        signal = _ema(diff, 9)
        result["macd"] = round(diff[-1], 3)
        result["macd_signal"] = round(signal[-1], 3)
        result["macd_hist"] = round(diff[-1] - signal[-1], 3)

    if n >= 20:
        window = closes[-20:]
        mid = sum(window) / 20
        sd = math.sqrt(sum((x - mid) ** 2 for x in window) / 20)
        result["boll_mid"] = round(mid, 3)
        result["boll_upper"] = round(mid + 2 * sd, 3)
        result["boll_lower"] = round(mid - 2 * sd, 3)

    return result
