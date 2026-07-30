"""Tests for the P0 finance tools (K-line / fundamentals / news)."""

import asyncio

from app.core.config import settings
from app.core.langgraph.tools import _finance_utils as fu
from app.core.langgraph.tools import fundamentals_tools, kline_tools, news_tools


def test_symbol_parsing():
    """Symbol parsers map bare codes and prefixed codes correctly."""
    assert fu.to_gtimg_symbol("600519") == "sh600519"
    assert fu.to_gtimg_symbol("sz000001") == "sz000001"
    assert fu.to_eastmoney_secid("600519") == "1.600519"
    assert fu.to_eastmoney_secid("sz000001") == "0.000001"


def test_technical_indicators():
    """Technical indicators (MA/RSI/MACD/BOLL) compute from a close series."""
    closes = list(range(1, 31))
    ind = fu.compute_technical_indicators(closes)
    assert ind["ma5"] is not None
    assert ind["rsi14"] is not None
    assert ind["macd"] is not None
    assert ind["boll_upper"] > ind["boll_mid"] > ind["boll_lower"]


def test_sentiment():
    """Sentiment scorer rises on bullish words and falls on bearish words."""
    assert fu.score_sentiment("") == 50
    assert fu.score_sentiment("大涨利好增长盈利") > 50
    assert fu.score_sentiment("大跌亏损风险警示") < 50


def test_tools_disabled_by_default(monkeypatch):
    """Every finance tool returns a disabled notice when its flag is off."""
    monkeypatch.setattr(settings, "ENABLE_KLINE", False)
    monkeypatch.setattr(settings, "ENABLE_FUNDAMENTALS", False)
    monkeypatch.setattr(settings, "ENABLE_NEWS", False)
    assert "未启用" in asyncio.run(kline_tools.fetch_kline.ainvoke({"symbol": "600519"}))
    assert "未启用" in asyncio.run(fundamentals_tools.fetch_fundamentals.ainvoke({"symbol": "600519"}))
    assert "未启用" in asyncio.run(news_tools.fetch_news_sentiment.ainvoke({"symbol": "600519"}))


async def _fake_http(url: str):
    if "qt.gtimg" in url:
        return 'v_sh600519="1~贵州茅台~600519~1685.0~1680.0~1690.0~1680.0~1696.0~1680.0~12345"'
    if "ifzq" in url or "appstock" in url:
        return {
            "data": {
                "sh600519": {
                    "qfqday": [
                        ["2024-01-02", "10", "11", "12", "9", "100"],
                        ["2024-01-03", "11", "12", "13", "10", "200"],
                        ["2024-01-04", "12", "13", "14", "11", "150"],
                        ["2024-01-05", "13", "14", "15", "12", "180"],
                        ["2024-01-08", "14", "15", "16", "13", "120"],
                    ]
                }
            }
        }
    if "np-anotice-stock" in url:
        return {
            "data": {
                "list": [
                    {"title": "公司营收大涨", "notice_date": "2024-01-05", "source_type": "东方财富"},
                    {"title": "行业风险警示", "notice_date": "2024-01-04", "source_type": "同花顺"},
                ]
            }
        }
    if "ZYZBAjaxNew" in url or "emweb" in url:
        return {
            "data": [
                {
                    "REPORT_DATE": "2024-03-31",
                    "EPSJB": 59.49,
                    "ROEJQ": 9.5,
                    "TOTALOPERATEREVE": 1234567,
                    "PARENTNETPROFIT": 123456,
                    "XSMLL": 91.0,
                    "ZCFZL": 20.0,
                }
            ]
        }
    return {}


def test_kline_parses(monkeypatch):
    """fetch_kline renders K-line + indicators from a mocked HTTP response."""
    monkeypatch.setattr(settings, "ENABLE_KLINE", True)
    monkeypatch.setattr(kline_tools, "ahttp_get_json", _fake_http)
    out = asyncio.run(kline_tools.fetch_kline.ainvoke({"symbol": "600519", "limit": "5"}))
    assert "K线" in out
    assert "MA5" in out
    assert "RSI" in out


def test_fundamentals_parses(monkeypatch):
    """fetch_fundamentals renders name + ROE + disclaimer from mocked data."""
    monkeypatch.setattr(settings, "ENABLE_FUNDAMENTALS", True)
    monkeypatch.setattr(fundamentals_tools, "ahttp_get_json", _fake_http)
    out = asyncio.run(fundamentals_tools.fetch_fundamentals.ainvoke({"symbol": "600519"}))
    assert "贵州茅台" in out
    assert "ROE" in out
    assert "不构成投资建议" in out


def test_news_parses(monkeypatch):
    """fetch_news_sentiment renders sentiment + titles from mocked news."""
    monkeypatch.setattr(settings, "ENABLE_NEWS", True)
    monkeypatch.setattr(news_tools, "ahttp_get_json", _fake_http)
    out = asyncio.run(news_tools.fetch_news_sentiment.ainvoke({"symbol": "600519", "limit": "5"}))
    assert "情绪" in out
    assert "营收大涨" in out
