"""TikZ 输出端后处理：让虚线压过与它重合的实线。

真实场景（某道正方体动点题）：模型画长方体时先
``\\draw (A)--(B)--(C)--(D)--cycle;`` 把底面画成实线，再补
``\\draw[dashed] (A)--(D);`` 表示被挡住的棱。两条线完全重合，
渲染出来一根虚线都没有 —— 图上分不清哪条棱在后头。TeX 自己也是这样，
只能在 SVG 输出端把重合的实线段删掉。
"""
from __future__ import annotations

from app.render.tikz import _polylines, dashed_over_solid

SOLID_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    '<path fill="none" d="M0 0L10 0L10 10L0 10Z"/>'
    '<path fill="none" stroke-dasharray="2,2" d="M0 10L0 0"/>'
    "</svg>"
)


def test_coincident_solid_segment_is_removed():
    out = dashed_over_solid(SOLID_SVG)
    solid = [
        p for p in out.split("<path")[1:]
        if "dasharray" not in p
    ]
    assert len(solid) == 1
    d = solid[0]
    # 原本四条边，与虚线重合的左边那条被删掉 → 只剩三段
    assert d.count("L") == 3
    assert "M0.000 0.000L10.000 0.000" in d
    # 虚线本身原样保留
    assert out.count("stroke-dasharray") == 1


def test_dashed_only_svg_is_untouched():
    svg = '<svg><path fill="none" stroke-dasharray="2,2" d="M0 0L1 1"/></svg>'
    assert dashed_over_solid(svg) == svg


def test_curves_are_left_alone():
    """含曲线命令的 path 一律不动，避免把二次/三次贝塞尔拆坏。"""
    svg = (
        '<svg><path fill="none" d="M0 0C1 1 2 2 3 3"/>'
        '<path fill="none" stroke-dasharray="2,2" d="M0 0L3 3"/></svg>'
    )
    assert dashed_over_solid(svg) == svg


def test_filled_paths_are_left_alone():
    svg = (
        '<svg><path fill="#000" d="M0 0L3 3L3 0Z"/>'
        '<path fill="none" stroke-dasharray="2,2" d="M0 0L3 3"/></svg>'
    )
    assert dashed_over_solid(svg) == svg


def test_polyline_parses_relative_commands():
    lines = _polylines("M0 0h10v10h-10z")
    assert lines == [
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    ]


def test_unknown_command_returns_none():
    assert _polylines("M0 0 Q1 1 2 2") is None
