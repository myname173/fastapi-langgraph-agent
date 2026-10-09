"""Research-debate tool for the chat stock-advisor (P2).

Runs a lightweight multi-agent-style debate in-process: the same cloud model
is prompted three times as bullish / bearish / neutral personas, then the
viewpoints are collected into a balanced summary. No new agent topology is
introduced — it is a pure LLM-service orchestration gated by
``ENABLE_RESEARCH_DEBATE``.
"""

from __future__ import annotations

import asyncio

from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging import logger
from app.services.llm import llm_service
from app.utils.graph import extract_text_content

ROLES = [
    ("看多视角", "你是一位偏乐观的股票分析师，重点寻找利好、成长空间与催化因素。"),
    ("看空视角", "你是一位审慎的股票分析师，重点寻找风险、估值泡沫与不利因素。"),
    ("中性视角", "你是一位中立的股票分析师，客观权衡多空，给出平衡判断。"),
]


@tool
async def research_debate(topic: str) -> str:
    """对投资标的或复杂问题进行多视角研究辩论（看多/看空/中性）.

    当用户需要"多角度分析/多方观点/研究辩论"等全面视角时调用。
    返回多方观点与综合结论，帮助更全面决策。

    Args:
        topic: 研究主题，如 "600519 当前是否值得持有"。

    Returns:
        多方观点 + 综合结论的 Markdown 文本。
    """
    if not settings.ENABLE_RESEARCH_DEBATE:
        return "⚠️ 研究辩论工具当前未启用（ENABLE_RESEARCH_DEBATE=false）。"

    views = []
    for role, persona in ROLES:
        try:
            resp = await asyncio.wait_for(
                llm_service.call(
                    [
                        {"role": "system", "content": persona},
                        {
                            "role": "user",
                            "content": f"主题：{topic}\n请给出你的核心观点（不超过200字）。",
                        },
                    ],
                    max_tokens=400,
                    temperature=0.1,
                    cacheable=False,
                    bind_tools=False,
                ),
                timeout=30,
            )
            views.append(f"### {role}\n{extract_text_content(resp.content).strip()}")
        except asyncio.TimeoutError:
            logger.warning("research_debate_view_timeout", role=role)
            views.append(f"### {role}\n（观点生成超时）")
        except Exception as e:
            logger.warning("research_debate_view_failed", role=role, error=str(e))
            views.append(f"### {role}\n（观点生成失败）")

    conclusion = (
        "\n\n### 综合结论\n以上为多方视角的独立研究观点，仅供参考，不构成投资建议。请结合自身风险偏好独立判断。"
    )
    return "\n\n".join(views) + conclusion


research_debate_tools_list = [research_debate]
