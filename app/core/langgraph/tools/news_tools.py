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
    result = payload.get("result")
    rows = []
    if isinstance(result, dict):
        rows = result.get("data") or []
    elif isinstance(result, list):
        rows = result
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = item.get("NEWS_TITLE") or item.get("TITLE") or ""
        summary = item.get("SUMMARY") or item.get("CONTENT") or ""
        date = item.get("PUBLISH_DATE") or item.get("DATE") or ""
        src = item.get("SOURCE") or item.get("MEDIANAME") or ""
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
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPT_WEB_NEWS"
        "&columns=NEWS_CODE,NEWS_TITLE,PUBLISH_DATE,SOURCE,SUMMARY"
        f"&filter=(SECURITY_CODE%3D%22{quote(code)}%22)"
        f"&pageSize={limit}&sortColumns=PUBLISH_DATE&sortTypes=-1"
        "&source=WEB&client=WEB"
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
