"""News & sentiment tool for the chat advisor.

Fetches recent company news from a public Eastmoney endpoint and scores each
item with a lightweight Chinese financial lexicon so the chat agent can gauge
market sentiment (the third pillar of the three-pillar framework) without an
external NLP service.
"""

from __future__ import annotations

from urllib.parse import quote

from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging import logger
from app.core.langgraph.tools._finance_utils import (
    ahttp_get_json,
    score_sentiment,
    to_eastmoney_secid,
)


def _parse_news(payload: dict) -> list[dict]:
    data = payload.get("data") or {}
    rows = data.get("list") or []
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("title_ch") or ""
        summary = item.get("summary") or ""
        date = item.get("notice_date") or item.get("display_time") or ""
        src = item.get("source_type") or ""
        out.append(
            {
                "title": title,
                "summary": summary,
                "date": str(date)[:10],
                "source": src,
                "score": score_sentiment((title or "") + (summary or "")),
            }
        )
    return out


@tool
async def fetch_news_sentiment(symbol: str, limit: int = 10) -> str:
    """获取个股近期新闻并做情绪评分（基本面/情绪面分析的输入）.

    当用户关心"最近有什么消息/新闻""市场情绪如何""有没有利好利空"时调用。
    返回新闻标题列表与情绪评分（0-100，越高越偏正面）。

    Args:
        symbol: 股票代码，支持 sh600519 / sz000001 / 600519 等格式。
        limit: 返回新闻条数，默认10（范围1-30）。

    Returns:
        包含新闻列表与情绪摘要的 Markdown 文本。
    """
    if not settings.ENABLE_NEWS:
        return "⚠️ 新闻情绪工具当前未启用（ENABLE_NEWS=false）。"

    limit = max(1, min(int(limit), 30))
    secid = to_eastmoney_secid(symbol)
    code = secid.split(".")[-1]
    # The legacy datacenter reportName (RPT_WEB_NEWS) was retired; the
    # np-anotice-stock announcement endpoint is the current source.
    url = (
        "https://np-anotice-stock.eastmoney.com/api/security/ann"
        "?sr=-1&page_index=1"
        f"&page_size={limit}"
        "&ann_type=A&client_source=web"
        f"&stock_list={quote(code)}"
        "&f_node=0&s_node=0"
    )
    try:
        payload = await ahttp_get_json(url)
        news = _parse_news(payload)
    except Exception as e:
        logger.warning("fetch_news_failed", symbol=symbol, error=str(e))
        return f"⚠️ 获取 {symbol} 新闻失败：{e}"

    if not news:
        return f"⚠️ 未获取到 {symbol} 的相关新闻。"

    avg = round(sum(n["score"] for n in news) / len(news), 1)
    mood = "偏正面" if avg >= 60 else "偏负面" if avg <= 40 else "中性"
    lines = [
        f"## {symbol} 近期新闻（{len(news)}条，平均情绪 {avg}/100，{mood}）",
        "",
        "| 日期 | 来源 | 标题 | 情绪 |",
        "| --- | --- | --- | --- |",
    ]
    for n in news:
        title = (n["title"] or n["summary"] or "")[:40]
        lines.append(f"| {n['date']} | {n['source']} | {title} | {n['score']} |")
    lines.append("\n⚠️ 以上新闻与情绪评分仅供参考，不构成投资建议。")
    return "\n".join(lines)


news_tools_list = [fetch_news_sentiment]
