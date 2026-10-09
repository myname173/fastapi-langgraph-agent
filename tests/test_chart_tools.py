"""Tests for the SVG chart tool (bar / pie).

Regression coverage for a bar-chart geometry bug: the previous implementation
scaled by ``max(values)`` and always drew bars upward, so an all-negative
series (e.g. losses / negative returns) produced bars with a negative ``y``
and a height far beyond the viewBox — i.e. invisible, out-of-frame bars.
"""

import re

from app.core.langgraph.tools.chart_tools import _svg_bar, _svg_pie

VIEWBOX_H = 300


def _rects(svg: str) -> list[tuple[int, int, int, int]]:
    """Extract (x, y, width, height) for every <rect> in the SVG."""
    return [
        (int(x), int(y), int(w), int(h))
        for x, y, w, h in re.findall(r'<rect x="(-?\d+)" y="(-?\d+)" width="(\d+)" height="(\d+)"', svg)
    ]


class TestSvgBar:
    """Every bar must stay inside the viewBox."""

    def test_all_negative_values_stay_in_frame(self):
        """All-negative bars must stay inside the viewBox."""
        svg = _svg_bar("涨幅对比", ["A", "B", "C"], [-2.5, -1.55, -6.01])
        rects = _rects(svg)
        assert len(rects) == 3
        for _x, y, _w, h in rects:
            assert y >= 0, f"bar starts above the viewBox (y={y})"
            assert y + h <= VIEWBOX_H, f"bar ends below the viewBox (y+h={y + h})"
            assert h > 0

    def test_negative_bars_are_proportional(self):
        """Bar height scales with the magnitude of each negative value."""
        svg = _svg_bar("t", ["A", "B", "C"], [-2.5, -1.55, -6.01])
        heights = [h for _x, _y, _w, h in _rects(svg)]
        # -6.01 has the largest magnitude => tallest bar; -1.55 => shortest.
        assert heights[2] == max(heights)
        assert heights[1] == min(heights)

    def test_mixed_values_share_a_zero_baseline(self):
        """Positive and negative bars share a single zero baseline."""
        svg = _svg_bar("t", ["up", "down"], [3.0, -2.0])
        up_y, up_h = _rects(svg)[0][1], _rects(svg)[0][3]
        down_y, down_h = _rects(svg)[1][1], _rects(svg)[1][3]
        # The positive bar ends exactly where the negative bar begins (zero line).
        assert up_y + up_h == down_y
        # 3.0 > |-2.0|, so the up bar is taller.
        assert up_h > down_h

    def test_positive_only_series_stays_in_frame(self):
        """A positive-only series stays inside the viewBox."""
        rects = _rects(_svg_bar("t", ["A", "B"], [1.0, 2.0]))
        assert rects
        assert all(y >= 0 and y + h <= VIEWBOX_H for _x, y, _w, h in rects)

    def test_empty_values_returns_empty_svg(self):
        """Empty input still yields a valid SVG with no bars."""
        svg = _svg_bar("t", [], [])
        assert "<svg" in svg
        assert "<rect" not in svg


class TestSvgPie:
    """A pie shows share of magnitude; negatives must not break the angles."""

    def test_negative_values_still_render(self):
        """Negative pie values fall back to equal slices instead of vanishing."""
        svg = _svg_pie("t", ["A", "B"], [-1.0, -3.0])
        assert svg.count("<path") == 2

    def test_all_zero_is_empty(self):
        """An all-zero series draws no pie slices."""
        assert "<path" not in _svg_pie("t", ["A"], [0.0])
