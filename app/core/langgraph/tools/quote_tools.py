"""Real-time market quote tool for the chat agent.

Provides a LangChain tool that fetches live quotes from the Tencent
Finance public API (http://qt.gtimg.cn). Used by the chat agent when
the user asks about current prices, intraday moves, or the latest
snapshot of an A-share / HK / US ticker.

Complementary to the knowledge base:

- The KB holds **historical** snapshots (e.g. "yesterday's close was
  1327.5") that the LLM can cite as reference data.
- This tool holds **live** data with second-level freshness, used
  when the user asks "现在多少钱" / "现在什么价" / "今天涨了多少".

Data source: ``http://qt.gtimg.cn/q=<symbols>`` — no auth, no API key.
Format: CSV with ~50 fields per symbol (price, change, volume, P/E, …).
The same script logic is used by ``test_data/generate_market_snapshot.py``
to build the KB snapshots, so behaviour is consistent.

Gated by ``ENABLE_REALTIME_QUOTES`` feature flag in the tool registry.
"""

from __future__ import annotations

import http.client
import time
import urllib.request
from typing import List

from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging import logger

# Tencent Finance qt.gtimg.cn endpoint. Free, no auth, public.
# Symbols are comma-separated; each returns one CSV line.
API_URL = "http://qt.gtimg.cn/q={symbols}"


def _fetch_raw(
    symbols: list[str],
    *,
    timeout: int,
    retries: int,
) -> dict[str, list[str]]:
    """Hit the Tencent Finance API once with retry/backoff.

    Args:
        symbols: List of symbol codes (``["sh000001", "sz300750"]``).
        timeout: HTTP timeout in seconds per attempt.
        retries: Total attempts before giving up.

    Returns:
        Dict mapping symbol → list of CSV fields. Symbols that the API
        did not return are silently dropped.
    """
    url = API_URL.format(symbols=",".join(symbols))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read().decode("gbk", errors="ignore")
            out: dict[str, list[str]] = {}
            for line in data.strip().split("\n"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                # Expected: v_sh000001="<field1>~<field2>~..."  (or v_hk00700, v_usDJI, …)
                key, _, value = line.partition("=")
                sym = key.strip().lstrip("v_")
                value = value.strip().strip(";").strip('"')
                if not value:
                    continue
                out[sym] = value.split("~")
            if out:
                return out
            last_err = ValueError("empty response from qt.gtimg.cn")
        except (http.client.RemoteDisconnected, ConnectionError, TimeoutError) as e:
            last_err = e
        except Exception as e:  # noqa: BLE001 — surface as soft failure to LLM
            last_err = e
        # Linear backoff before retrying.
        if attempt < retries - 1:
            time.sleep(0.5 * (attempt + 1))
    logger.warning(
        "quote_fetch_failed",
        symbols=symbols,
        attempts=retries,
        error=str(last_err) if last_err else "unknown",
    )
    return {}


def _format_row(symbol: str, name_hint: str, fields: list[str]) -> str:
    """Render one quote row as a Markdown table line.

    Field layout (Tencent format, indices and stocks share the same):
      [0]  market type     [1] name           [2] code
      [3]  current_price   [4] prev_close     [5] open
      [30] datetime       [31] change         [32] change_pct
      [33] high           [34] low            [35] volume/amount

    For commodity (``hf_*``), the format is a single CSV (no tilde).

    Args:
        symbol: Symbol code (e.g. ``sh600519``).
        name_hint: Fallback name when the API did not return a name
            field (e.g. user passes an unknown ticker).
        fields: List of CSV fields from the API.

    Returns:
        Markdown table row ``| code | name | price | change | pct | high/low | time |``.
    """
    # Commodity path — single CSV, no tilde-separated fields.
    if symbol.startswith("hf_") and fields and "~" not in fields[0]:
        parts = [p.strip() for p in fields[0].split(",")]
        if len(parts) < 7:
            return f"| {symbol} | {name_hint} | (字段不足) | - | - | - | - |"
        return f"| {symbol} | {name_hint} | {parts[0]} | {parts[1]} | - | 高 {parts[4]} / 低 {parts[5]} | {parts[6]} |"

    if len(fields) < 35:
        return f"| {symbol} | {name_hint} | (字段不足) | - | - | - | - |"
    name = fields[1] or name_hint
    price = fields[3]
    change = fields[31]
    pct = fields[32]
    high = fields[33]
    low = fields[34]
    timestamp = fields[30] if len(fields) > 30 else "-"
    return f"| {symbol} | {name} | {price} | {change} | {pct}% | 高 {high} / 低 {low} | {timestamp} |"


@tool
async def fetch_realtime_quote(symbols: List[str]) -> str:
    """Fetch real-time stock/index quotes from Tencent Finance.

    Use this tool ONLY when the user asks about current, live, or
    "现在/最新/实时" prices. For historical context, sector themes, or
    methodology questions, prefer ``search_knowledge_base`` instead.

    Supports A-share (``sh600519``, ``sz300750``), HK (``hk00700``),
    US stocks and indices (``usAAPL``, ``usDJI``), and commodities
    (``hf_GC`` gold, ``hf_CL`` crude oil). FX rates (USDCNY etc.) are
    not supported by the upstream API — if asked, say so and direct
    the user to a forex data source.

    Args:
        symbols: List of ticker codes. Up to
            ``settings.QUOTE_MAX_SYMBOLS_PER_CALL`` per call. Prefix
            must be in ``settings.QUOTE_ALLOWED_PREFIXES``; otherwise
            the call is rejected with an error message.

    Returns:
        Markdown table with one row per successfully fetched symbol.
        Missing symbols show as ``(无数据)`` in the table.
    """
    if not settings.ENABLE_REALTIME_QUOTES:
        return (
            "实时行情功能已被禁用（ENABLE_REALTIME_QUOTES=false）。"
            "如需启用，请在环境变量中设置 ENABLE_REALTIME_QUOTES=true 并重启服务。"
        )

    if not symbols:
        return "请提供至少一个股票代码（例如 sh600519、hk00700、usAAPL）。"

    # Normalize + dedupe, preserving order.
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        if raw is None:
            continue
        s = str(raw).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        normalized.append(s)

    if not normalized:
        return "请提供至少一个股票代码（例如 sh600519、hk00700、usAAPL）。"

    # Whitelist enforcement.
    bad = [s for s in normalized if not any(s.startswith(p) for p in settings.QUOTE_ALLOWED_PREFIXES)]
    if bad:
        return (
            f"以下代码前缀不在允许列表中：{bad}。"
            f"允许的前缀：{settings.QUOTE_ALLOWED_PREFIXES}。"
            "如需新增前缀，请修改 settings.QUOTE_ALLOWED_PREFIXES。"
        )

    # Per-call size cap.
    if len(normalized) > settings.QUOTE_MAX_SYMBOLS_PER_CALL:
        return (
            f"单次最多查询 {settings.QUOTE_MAX_SYMBOLS_PER_CALL} 个代码，当前传入 {len(normalized)} 个。请分批调用。"
        )

    # Synchronous urllib call is fast (< 500ms typical); no need to
    # offload to a thread for typical small batches.
    fetched = _fetch_raw(
        normalized,
        timeout=settings.QUOTE_HTTP_TIMEOUT_SECONDS,
        retries=settings.QUOTE_HTTP_RETRIES,
    )

    rows: list[str] = []
    missing: list[str] = []
    for sym in normalized:
        if sym in fetched:
            rows.append(_format_row(sym, sym, fetched[sym]))
        else:
            missing.append(sym)
            rows.append(f"| {sym} | (无数据) | - | - | - | - | - |")

    header = (
        "| 代码 | 名称 | 现价 | 涨跌 | 涨跌幅 | 高低 | 时间 |\n|------|------|------|------|--------|------|------|"
    )
    table = "\n".join([header, *rows])

    notice = ""
    if missing:
        notice = f"\n\n> 注意：{len(missing)} 个代码无数据（{missing}），可能是非交易时段或代码错误，请核对。"
    elif not fetched:
        notice = "\n\n> 注意：本次请求 qt.gtimg.cn 全部失败，可能是网络问题或非交易时段，请稍后重试或核对网络。"

    logger.info(
        "realtime_quote_fetched",
        requested=len(normalized),
        succeeded=len(fetched),
        missing=len(missing),
    )
    return table + notice


quote_tools_list = [fetch_realtime_quote]
