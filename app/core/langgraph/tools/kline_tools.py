"""Technical-analysis tool: K-line data + indicators for the chat advisor.

Uses the public Tencent (gtimg) K-line endpoint (no token required) and
computes MA / RSI / MACD / BOLL in pure Python so the chat agent can discuss
support/resistance and trend state without external indicator libraries.
"""

from __future__ import annotations

from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging import logger
from app.core.langgraph.tools._finance_utils import (
    ahttp_get_json,
    compute_technical_indicators,
    to_gtimg_symbol,
)

KLINE_PERIODS = {"daily": "day", "weekly": "week", "monthly": "month"}


def _parse_kline_payload(payload: dict, gsym: str) -> list[dict]:
    data = payload.get("data") or {}
    sym_data = data.get(gsym) or {}
    raw = sym_data.get("qfqday") or sym_data.get("day") or []
    rows = []
    for item in raw:
        if not isinstance(item, list) or len(item) < 6:
            continue
        date, _open, close, high, low, volume = item[:6]
        try:
            rows.append(
                {
                    "date": date,
                    "open": float(_open),
                    "close": float(close),
                    "high": float(high),
                    "low": float(low),
                    "volume": float(volume),
                }
            )
        except (ValueError, TypeError):
            continue
    return rows


@tool
async def fetch_kline(symbol: str, period: str = "daily", limit: int = 60) -> str:
    """获取股票K线数据与技术指标（MA/MACD/RSI/BOLL）.

    当用户讨论技术面、趋势、买卖点、支撑/压力位时调用。
    返回的K线和技术指标帮助判断多空状态。

    Args:
        symbol: 股票代码，支持 sh600519 / sz000001 / 600519 等格式。
        period: 周期，daily(日,默认)/weekly(周)/monthly(月)。
        limit: 返回最近多少根K线，默认60（范围5-240）。

    Returns:
        包含最近K线与技术指标的 Markdown 文本。
    """
    if not settings.ENABLE_KLINE:
        return "⚠️ 技术面分析工具当前未启用（ENABLE_KLINE=false）。"

    period = period if period in KLINE_PERIODS else "daily"
    limit = max(5, min(int(limit), 240))
    gsym = to_gtimg_symbol(symbol)
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={gsym},{KLINE_PERIODS[period]},,,{limit},qfq"
    try:
        payload = await ahttp_get_json(url)
        rows = _parse_kline_payload(payload, gsym)
    except Exception as e:
        logger.warning("fetch_kline_failed", symbol=symbol, error=str(e))
        return f"⚠️ 获取 {symbol} K线失败：{e}"

    if not rows:
        return f"⚠️ 未获取到 {symbol} 的K线数据（可能代码格式不正确）。"

    closes = [r["close"] for r in rows]
    ind = compute_technical_indicators(closes)
    last = rows[-1]

    lines = [
        f"## {symbol} {period} K线（最近{len(rows)}根）",
        "",
        "| 日期 | 收盘 | 最高 | 最低 | 成交量(手) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rows[-15:]:
        lines.append(f"| {r['date']} | {r['close']} | {r['high']} | {r['low']} | {r['volume']} |")
    lines += [
        "",
        "### 技术指标",
        f"- 最新收盘：{last['close']}（最高 {last['high']} / 最低 {last['low']}）",
        f"- MA5：{ind['ma5']}  MA10：{ind['ma10']}  MA20：{ind['ma20']}",
        f"- RSI(14)：{ind['rsi14']}",
        f"- MACD：{ind['macd']}（信号 {ind['macd_signal']}，柱 {ind['macd_hist']}）",
        f"- BOLL(20,2)：上轨 {ind['boll_upper']} / 中轨 {ind['boll_mid']} / 下轨 {ind['boll_lower']}",
    ]
    return "\n".join(lines)


kline_tools_list = [fetch_kline]
