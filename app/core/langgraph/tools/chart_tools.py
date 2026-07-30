"""Chart-generation tool for the chat stock-advisor.

Generates simple, dependency-free SVG charts (bar / pie) so the advisor can
visualize allocation, PnL, or indicator comparisons without pulling in a
plotting library. The SVG file path is returned; the UI can render it.

Gated by ``ENABLE_CHART_GEN`` so it stays inert unless explicitly enabled.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from langchain_core.tools import tool

from app.core.config import settings

CHART_DIR = Path(os.getenv("CHART_OUTPUT_DIR", str(Path(tempfile.gettempdir()) / "generated_charts")))


def _parse_data(data: str) -> tuple[list[str], list[float]]:
    try:
        obj = json.loads(data)
        labels = [str(x) for x in obj.get("labels", [])]
        values = [float(x) for x in obj.get("values", [])]
    except (ValueError, TypeError):
        labels, values = [], []
    return labels, values


def _svg_bar(title: str, labels: list[str], values: list[float]) -> str:
    w, h = 480, 300
    if not values:
        return f'<svg width="{w}" height="{h}"></svg>'
    max_v = max(values) or 1.0
    n = len(values)
    gap = 20
    bw = (w - 2 * gap - (n - 1) * 10) / max(n, 1)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">']
    parts.append(f'<text x="{w // 2}" y="20" text-anchor="middle">{title}</text>')
    base_y = h - 40
    for i, (lab, val) in enumerate(zip(labels, values, strict=False)):
        x = gap + i * (bw + 10)
        bh = (val / max_v) * (base_y - 40)
        parts.append(f'<rect x="{x:.0f}" y="{base_y - bh:.0f}" width="{bw:.0f}" height="{bh:.0f}" fill="#3b82f6" />')
        parts.append(f'<text x="{x + bw / 2:.0f}" y="{base_y + 16}" text-anchor="middle" font-size="10">{lab}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_pie(title: str, labels: list[str], values: list[float]) -> str:
    w = h = 320
    if not values or sum(values) <= 0:
        return f'<svg width="{w}" height="{h}"></svg>'
    cx, cy, r = w // 2, h // 2 + 10, 110
    total = sum(values)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">']
    parts.append(f'<text x="{w // 2}" y="20" text-anchor="middle">{title}</text>')
    colors = ["#3b82f6", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6", "#06b6d4"]
    angle = -math.pi / 2
    for i, (_lab, val) in enumerate(zip(labels, values, strict=False)):
        frac = val / total
        a2 = angle + 2 * math.pi * frac
        large = 1 if 2 * math.pi * frac > math.pi else 0
        x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        x2, y2 = cx + r * math.cos(a2), cy + r * math.sin(a2)
        parts.append(
            f'<path d="M{cx} {cy} L{x1:.1f} {y1:.1f} A{r} {r} 0 {large} 1 '
            f'{x2:.1f} {y2:.1f} Z" fill="{colors[i % len(colors)]}" />'
        )
        angle = a2
    # Legend
    for i, lab in enumerate(labels):
        y = h - 12 - (len(labels) - 1 - i) * 14
        parts.append(f'<rect x="10" y="{y - 8}" width="10" height="10" fill="{colors[i % len(colors)]}" />')
        parts.append(f'<text x="24" y="{y}" font-size="10">{lab}</text>')
    parts.append("</svg>")
    return "".join(parts)


@tool
async def generate_chart(chart_type: str, title: str, data: str) -> str:
    """生成图表（SVG）用于可视化持仓占比、盈亏对比等.

    当用户想要"画个图/可视化/占比分布"或分析持仓配置时调用。
    返回图表文件保存路径，UI 可渲染。

    Args:
        chart_type: 图表类型，bar(柱状图)/pie(饼图)。
        title: 图表标题。
        data: JSON 字符串，格式 {"labels":[...], "values":[...]}（数值）。

    Returns:
        图表说明与 SVG 文件路径（Markdown）。
    """
    if not settings.ENABLE_CHART_GEN:
        return "⚠️ 图表生成工具当前未启用（ENABLE_CHART_GEN=false）。"

    labels, values = _parse_data(data)
    if not labels or not values:
        return '⚠️ 图表数据解析失败，请提供 {"labels":[...],"values":[...]}。'

    svg = _svg_pie(title, labels, values) if chart_type == "pie" else _svg_bar(title, labels, values)

    CHART_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"chart_{hashlib.md5((title + chart_type).encode()).hexdigest()[:8]}.svg"
    path = CHART_DIR / fname
    try:
        path.write_text(svg, encoding="utf-8")
    except Exception as e:
        return f"⚠️ 图表保存失败：{e}"

    return (
        f"已生成{('饼图' if chart_type == 'pie' else '柱状图')}：{title}\n"
        f"标签：{', '.join(labels)}\n"
        f"数值：{', '.join(str(v) for v in values)}\n"
        f"文件：{path}"
    )


chart_tools_list = [generate_chart]
