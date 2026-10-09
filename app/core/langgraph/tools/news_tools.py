"""News & sentiment tool for the chat advisor.

Fetches recent company announcements (per-stock) or industry flash news
(keyword) from public Eastmoney endpoints and scores each item with a
lightweight Chinese financial lexicon so the chat agent can gauge market
sentiment (the third pillar of the three-pillar framework) without an
external NLP service.

Two modes:
- ``symbol`` given  -> company announcements (np-anotice-stock ann endpoint).
- ``keyword`` given -> industry flash news (np-listapi 7x24 getFastNewsList),
  filtered client-side by the keyword. This exists so sector questions
  ("半导体有什么消息") return sector-relevant news instead of falling back
  to a stock code drawn from long-term memory — the root cause of the
  "asked 半导体, answered 茅台" regression.
"""

from __future__ import annotations

import time
from urllib.parse import quote

from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging import logger
from app.core.langgraph.tools._finance_utils import (
    ahttp_get_json,
    score_sentiment,
    to_eastmoney_secid,
)

# Suffixes stripped from a keyword before matching flash news. Asking about
# "半导体行业" / "芯片板块" / "新能源概念股" should still match flash news that
# only mentions "半导体" / "芯片" / "新能源".
_KEYWORD_SUFFIXES = ("行业", "板块", "概念股", "概念", "主题", "赛道", "产业链", "股")

# How many flash-news items to fetch before client-side filtering. Flash news
# is global/macro, so a single page rarely yields many sector hits — pull a
# larger window (50) and filter down to ``limit``.
_FAST_NEWS_SCAN_SIZE = 50


def _normalize_keyword(keyword: str) -> str:
    """Strip common sector suffixes so matching uses the core term."""
    k = (keyword or "").strip()
    for suf in _KEYWORD_SUFFIXES:
        if k.endswith(suf) and len(k) > len(suf):
            k = k[: -len(suf)]
            break
    return k.strip()


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
        # source_type is a market/board code (e.g. 31/331/333), not a media
        # name. The announcement category in columns[0].column_name is the
        # readable, useful signal (e.g. "调研活动", "回购预案").
        cols = item.get("columns") or []
        category = cols[0].get("column_name") if cols and isinstance(cols[0], dict) else ""
        out.append(
            {
                "title": title,
                "summary": summary,
                "date": str(date)[:10],
                "category": category or "公告",
                "score": score_sentiment((title or "") + (summary or "")),
            }
        )
    return out


def _parse_fast_news(payload: dict, keyword: str, limit: int) -> list[dict]:
    """Parse 7x24 flash news, keeping only items matching ``keyword``."""
    core = _normalize_keyword(keyword)
    data = payload.get("data") or {}
    rows = data.get("fastNewsList") or []
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        summary = item.get("summary") or ""
        text = title + summary
        # Substring match on the core term; flash news is macro so this keeps
        # items like "费城半导体指数涨6%" for keyword="半导体".
        if core and core not in text:
            continue
        show_time = item.get("showTime") or ""
        out.append(
            {
                "title": title,
                "summary": summary,
                "date": str(show_time)[:10],
                "category": "快讯",
                "score": score_sentiment(text),
            }
        )
        if len(out) >= limit:
            break
    return out


async def _fetch_industry_news(keyword: str, limit: int) -> list[dict]:
    """Fetch 7x24 flash news and filter by ``keyword`` (client-side)."""
    url = (
        "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
        "?client=web&biz=web_724&fastColumn=102&sortEnd="
        f"&pageSize={_FAST_NEWS_SCAN_SIZE}&page_index=1"
        f"&req_trace={int(time.time() * 1000)}"
    )
    payload = await ahttp_get_json(url)
    return _parse_fast_news(payload, keyword, limit)


def _render_news(header: str, news: list[dict]) -> str:
    """Render a list of parsed news items as a Markdown table + sentiment."""
    if not news:
        return f"⚠️ 未获取到 {header} 的相关新闻。"
    avg = round(sum(n["score"] for n in news) / len(news), 1)
    mood = "偏正面" if avg >= 60 else "偏负面" if avg <= 40 else "中性"
    lines = [
        f"## {header}（{len(news)}条，平均情绪 {avg}/100，{mood}）",
        "",
        "| 日期 | 类别 | 标题 | 情绪 |",
        "| --- | --- | --- | --- |",
    ]
    for n in news:
        title = (n["title"] or n["summary"] or "")[:40]
        lines.append(f"| {n['date']} | {n['category']} | {title} | {n['score']} |")
    lines.append("\n⚠️ 以上新闻与情绪评分仅供参考，不构成投资建议。")
    return "\n".join(lines)


@tool
async def fetch_news_sentiment(
    symbol: str | None = None,
    keyword: str | None = None,
    limit: int = 10,
) -> str:
    """获取个股公告或行业快讯并做情绪评分（基本面/情绪面分析的输入）.

    两种模式（二选一，同时给出时以 symbol 为准）：
    - 传入 ``symbol``：获取该个股的近期公告（东方财富 ann 接口）。
    - 传入 ``keyword``：获取该行业/主题的近期快讯（东方财富 7x24 快讯接口，
      按关键词过滤）。当用户询问的是行业或主题（如"半导体""新能源""芯片"
      有什么消息）而非具体个股时，使用此模式，**不要**传 symbol。

    当用户关心"最近有什么消息/新闻""市场情绪如何""有没有利好利空"时调用。
    返回新闻标题、类别与情绪评分（0-100，越高越偏正面）。

    Args:
        symbol: 股票代码，支持 sh600519 / sz000001 / 600519 等格式。与
            keyword 二选一；同时给出时以 symbol 为准（个股公告优先）。
        keyword: 行业/主题关键词，如"半导体""芯片""新能源"。仅当未提供
            symbol 时生效。
        limit: 返回新闻条数，默认10（范围1-30）。

    Returns:
        包含新闻列表与情绪摘要的 Markdown 文本。
    """
    if not settings.ENABLE_NEWS:
        return "⚠️ 新闻情绪工具当前未启用（ENABLE_NEWS=false）。"

    limit = max(1, min(int(limit), 30))

    # Industry/keyword mode: sector flash news, client-side filtered. This
    # prevents the LLM from substituting a stock code from memory when the
    # user asks about a sector (e.g. "半导体" ->茅台 regression).
    if not symbol and keyword:
        if not _normalize_keyword(keyword):
            return "⚠️ 请提供有效的股票代码或行业关键词。"
        try:
            news = await _fetch_industry_news(keyword, limit)
        except Exception as e:
            logger.warning("fetch_industry_news_failed", keyword=keyword, error=str(e))
            return f"⚠️ 获取 {keyword} 行业新闻失败：{e}"
        return _render_news(f"{keyword} 行业快讯", news)

    if not symbol:
        return "⚠️ 请提供股票代码或行业关键词。"

    # Per-stock announcement mode (original path).
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

    return _render_news(f"{symbol} 近期公告", news)


news_tools_list = [fetch_news_sentiment]
