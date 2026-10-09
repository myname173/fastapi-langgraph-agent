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

from langchain_core.tools import tool

from app.core.config import settings

# Charts are written into the stable, statically-served directory so the
# frontend can render them via a predictable URL (/charts/<file>.svg).
CHART_DIR = settings.CHART_DIR


def _parse_data(data: str) -> tuple[list[str], list[float]]:
    try:
        obj = json.loads(data)
        labels = [str(x) for x in obj.get("labels", [])]
        values = [float(x) for x in obj.get("values", [])]
    except (ValueError, TypeError):
        labels, values = [], []
    return labels, values


def _svg_bar(title: str, labels: list[str], values: list[float]) -> str:
    """Render a bar chart around a shared zero baseline.

    Bars grow up from (positive) or down to (negative) a common zero line,
    so losses / negative returns are drawn *below* the baseline. The old
    implementation scaled by ``max(values)`` and always drew upward, which
    for an all-negative series produced negative ``y`` and a height far
    taller than the viewBox (bars clipped out of frame).
    """
    w, h = 480, 300
    if not values:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"></svg>'

    n = len(values)
    gap = 20
    top, bottom = 44, h - 40  # plot area; leaves room for title + x labels
    plot_h = bottom - top

    # Always include zero so the baseline is meaningful; guard a flat series.
    hi = max(max(values), 0.0)
    lo = min(min(values), 0.0)
    span = (hi - lo) or 1.0

    def y_of(v: float) -> float:
        return top + (hi - v) / span * plot_h

    zero_y = y_of(0.0)
    bw = (w - 2 * gap - (n - 1) * 10) / max(n, 1)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">']
    parts.append(f'<text x="{w // 2}" y="20" text-anchor="middle">{title}</text>')
    parts.append(
        f'<line x1="{gap}" y1="{zero_y:.1f}" x2="{w - gap}" y2="{zero_y:.1f}" stroke="#94a3b8" stroke-width="1" />'
    )

    for i, (lab, val) in enumerate(zip(labels, values, strict=False)):
        x = gap + i * (bw + 10)
        yv = y_of(val)
        y_top = min(yv, zero_y)
        bar_h = abs(yv - zero_y)
        # Match the app's own convention (.fin-up = green, .fin-down = red).
        fill = "#2dd4bf" if val >= 0 else "#f87171"
        parts.append(f'<rect x="{x:.0f}" y="{y_top:.0f}" width="{bw:.0f}" height="{bar_h:.0f}" fill="{fill}" />')
        # Value label sits just past the bar's outer end: above the bar for
        # positives, inside the bar (near its bottom) for negatives, so it
        # never collides with the x-axis label.
        vy = yv - 6
        parts.append(
            f'<text x="{x + bw / 2:.0f}" y="{vy:.0f}" text-anchor="middle" '
            f'font-size="10" fill="#475569">{val:g}</text>'
        )
        parts.append(
            f'<text x="{x + bw / 2:.0f}" y="{bottom + 16}" text-anchor="middle" '
            f'font-size="10" fill="#334155">{lab}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_pie(title: str, labels: list[str], values: list[float]) -> str:
    w = h = 320
    if not values:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"></svg>'
    cx, cy, r = w // 2, h // 2 + 10, 110
    # A pie shows each slice's share of the whole; use |value| so negative
    # numbers (e.g. losses) still render instead of producing negative angles.
    total = sum(abs(v) for v in values)
    if total <= 0:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"></svg>'
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">']
    parts.append(f'<text x="{w // 2}" y="20" text-anchor="middle">{title}</text>')
    colors = ["#3b82f6", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6", "#06b6d4"]
    angle = -math.pi / 2
    for i, (_lab, val) in enumerate(zip(labels, values, strict=False)):
        frac = abs(val) / total
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

    url = f"/charts/{fname}"
    return (
        f"已生成{('饼图' if chart_type == 'pie' else '柱状图')}：{title}\n\n"
        f"![{title}]({url})\n\n"
        f"> 数据标签：{', '.join(labels)}｜数值：{', '.join(str(v) for v in values)}"
    )


chart_tools_list = [generate_chart]
