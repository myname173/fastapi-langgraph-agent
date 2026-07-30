"""Fundamental-analysis tool: valuation + financial statements summary.

Combines a live quote (Tencent gtimg, same source as ``fetch_realtime_quote``)
for valuation and the Eastmoney F10 main-financials endpoint for ROE / EPS /
revenue / profit / margins / leverage. Both sources are fetched defensively —
a failure in either returns a partial result rather than crashing the turn.
"""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging import logger
from app.core.langgraph.tools._finance_utils import (
    ahttp_get_json,
    to_eastmoney_secid,
    to_gtimg_symbol,
)


def _extract_valuation(realtime_quote: list) -> dict:
    def num(idx: int) -> Optional[float]:
        try:
            v = realtime_quote[idx].strip()
            return float(v) if v not in ("", "--", "None", "0.00") else None
        except (IndexError, ValueError):
            return None

    # Candidate field indices across quote variants for PE / PB.
    return {
        "pe": num(38) or num(39),
        "pb": num(44) or num(46),
    }


def _extract_financials(payload: dict) -> dict:
    rows = payload.get("data") or []
    data = rows[0] if isinstance(rows, list) and rows else {}

    def g(*keys: str) -> Any:
        for k in keys:
            if k in data and data[k] not in (None, "", "--"):
                return data[k]
        return None

    return {
        "report_date": g("REPORT_DATE", "DATE"),
        "eps": g("EPSJB", "BASIC_EPS", "EPS"),
        "roe": g("ROEJQ", "WEIGHTAVG_ROE", "ROE"),
        "revenue": g("TOTALOPERATEREVE", "YYSR", "OPERATE_INCOME"),
        "net_profit": g("PARENTNETPROFIT", "JLR", "PARENT_NETPROFIT"),
        "gross_margin": g("XSMLL", "GROSS_MARGIN"),
        "net_margin": g("JLL", "NET_MARGIN"),
        "debt_ratio": g("ZCFZL", "DEBT_RATIO"),
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


@tool
async def fetch_fundamentals(symbol: str) -> str:
    """获取股票基本面与估值数据（PE/PB/ROE/营收/净利润/毛利率/负债率）.

    当用户讨论公司质地、估值高低、财报表现、是否"便宜/贵"时调用。
    数据来自行情接口与财报接口，仅供参考，需结合行业与公司阶段判断。

    Args:
        symbol: 股票代码，支持 sh600519 / sz000001 / 600519 等格式。

    Returns:
        包含估值与核心财务指标的 Markdown 文本。
    """
    if not settings.ENABLE_FUNDAMENTALS:
        return "⚠️ 基本面分析工具当前未启用（ENABLE_FUNDAMENTALS=false）。"

    gsym = to_gtimg_symbol(symbol)
    secid = to_eastmoney_secid(symbol)
    # ZYZBAjaxNew expects an exchange-prefixed code (SH600519 / SZ000001).
    _market, _code = secid.split(".", 1)
    em_code = ("SH" if _market == "1" else "SZ") + _code
    lines = [f"## {symbol} 基本面与估值", ""]

    # Valuation via live quote (same source as fetch_realtime_quote).
    quote_url = "https://qt.gtimg.cn/q=" + gsym + "&_=" + "0"
    try:
        raw = await ahttp_get_json(quote_url)
        text = raw if isinstance(raw, str) else str(raw)
        # The gtimg q= endpoint returns JS-assignment text; parse the payload.
        payload = text.split("=", 1)[1].strip().rstrip(";").strip('"')
        quote = payload.split("~")
        valuation = _extract_valuation(quote)
        lines += [
            "### 估值（行情口径）",
            f"- 股票名称：{quote[1] if len(quote) > 1 else '—'}",
            f"- 最新价：{quote[3] if len(quote) > 3 else '—'}",
            f"- 市盈率 PE：{valuation['pe'] or '—'}",
            f"- 市净率 PB：{valuation['pb'] or '—'}",
            "",
        ]
    except Exception as e:
        logger.warning("fetch_fundamentals_quote_failed", symbol=symbol, error=str(e))
        lines.append("### 估值：暂不可用\n")

    # Financials via Eastmoney F10 main-financials endpoint. The legacy
    # datacenter reportName (RPT_F10_FN_MAINFINANCE) was retired; the
    # PC_HSF10 ZYZBAjaxNew endpoint is the current source.
    fin_url = f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew?type=0&code={em_code}"
    try:
        payload = await ahttp_get_json(fin_url)
        fin = _extract_financials(payload)
        lines += [
            "### 核心财务",
            f"- 报告期：{fin['report_date'] or '—'}",
            f"- EPS（基本）：{_fmt(fin['eps'])}",
            f"- ROE（加权）：{_fmt(fin['roe'])}%",
            f"- 营业收入：{_fmt(fin['revenue'])}",
            f"- 净利润：{_fmt(fin['net_profit'])}",
            f"- 毛利率：{_fmt(fin['gross_margin'])}%",
            f"- 净利率：{_fmt(fin['net_margin'])}%",
            f"- 资产负债率：{_fmt(fin['debt_ratio'])}%",
        ]
    except Exception as e:
        logger.warning("fetch_fundamentals_f10_failed", symbol=symbol, error=str(e))
        lines.append("### 核心财务：暂不可用")

    lines.append("\n⚠️ 以上数据仅供参考，不构成投资建议，请以交易所/公司公告为准。")
    return "\n".join(lines)


fundamentals_tools_list = [fetch_fundamentals]
