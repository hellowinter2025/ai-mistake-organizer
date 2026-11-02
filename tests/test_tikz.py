"""TikZ 渲染链路测试。

这些用例会真的调 node 编译（单张 0.5~1.6s），所以只挑最有代表性的几种：
一个能画对的三角形、一段坏语法、一段危险命令。
没装 node 时整体跳过——题图渲染是增强能力，缺了不影响主流程。
"""
from __future__ import annotations

import shutil

import pytest

from app.render import tikz

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 运行 TikZ 引擎")

TRIANGLE = r"""\begin{tikzpicture}[scale=1]
  \draw[thick] (0,0) -- (6,0) -- (3,4) -- cycle;
  \draw[dashed] (3,0) -- (3,4);
  \draw (3,0) rectangle (3.4,0.4);
  \node[below left] at (0,0) {$B$};
  \node[below right] at (6,0) {$C$};
  \node[above] at (3,4) {$A$};
\end{tikzpicture}"""


def test_normalise_extracts_tikzpicture_from_noise():
    """模型常把整段 document / Markdown 围栏一起吐出来，只留 tikzpicture。"""
    raw = "```latex\n\\documentclass{article}\n\\begin{document}\n" + TRIANGLE + "\n\\end{document}\n```"
    body = tikz.normalise(raw)
    assert body.startswith("\\begin{tikzpicture}")
    assert body.endswith("\\end{tikzpicture}")
    assert "documentclass" not in body


def test_normalise_wraps_bare_draw_commands():
    """只写了绘图命令也能用，自动补环境。"""
    body = tikz.normalise(r"\draw (0,0) -- (1,1);")
    assert body.startswith("\\begin{tikzpicture}")
    assert "\\draw (0,0) -- (1,1);" in body


def test_normalise_rejects_dangerous_commands():
    """TeX 能读写文件、跑 shell：这些命令一律拒绝。"""
    for bad in (
        r"\begin{tikzpicture}\write18{dir}\end{tikzpicture}",
        r"\begin{tikzpicture}\input{/etc/passwd}\end{tikzpicture}",
        r"\begin{tikzpicture}\usepackage{tikz}\end{tikzpicture}",
    ):
        with pytest.raises(tikz.TikzError):
            tikz.normalise(bad)


def test_normalise_repairs_unclosed_and_rejects_empty():
    """没闭合不再直接判死：补上 \\end{tikzpicture} 让它能编译（截断是常态）。"""
    fixed = tikz.normalise(r"\begin{tikzpicture}\draw (0,0)--(1,1);")
    assert fixed.startswith("\\begin{tikzpicture}")
    assert fixed.endswith("\\end{tikzpicture}")
    with pytest.raises(tikz.TikzError):
        tikz.normalise("   ")


def test_render_produces_svg_with_viewbox():
    result = tikz.render(TRIANGLE)
    assert result.svg.lstrip().startswith("<svg")
    assert "viewBox" in result.svg
    # TeX 画出来的必须是真图元，不是空壳
    assert "<path" in result.svg or "<line" in result.svg


def test_render_result_is_cached():
    tikz.render(TRIANGLE)
    again = tikz.render(TRIANGLE)
    assert again.cached is True
    assert again.ms == 0


def test_render_bad_syntax_raises():
    with pytest.raises(tikz.TikzError):
        tikz.render(r"\begin{tikzpicture}\draw (0,0)--;\end{tikzpicture}")


def test_pgfplots_is_only_loaded_when_used():
    """纯几何图不该为 pgfplots 多付 0.5s。"""
    assert tikz.needs_pgfplots(r"\begin{axis}\addplot {x};\end{axis}") is True
    assert tikz.needs_pgfplots(TRIANGLE) is False


# --- 折行残留清洗（真实踩坑，2026-10-30）-------------------------------------
# 模型输出的 TikZ 被固定宽度折行劈开后：(1) 换行变成字面 \n 挤成一行；
# (2) 坐标名 B1 被补成 "B 1"。两者都会让 TikZ 编译直接失败。

def test_clean_tikz_restores_literal_newline():
    from app.ai.parser import clean_tikz

    out = clean_tikz(r"\begin{tikzpicture}\n\draw (0,0) -- (1,1);\n\end{tikzpicture}")
    assert "\\n\\draw" not in out
    assert out.splitlines()[0] == "\\begin{tikzpicture}"


def test_clean_tikz_keeps_commands_starting_with_n():
    """``\\node`` / ``\\tikz`` 不能被当成字面换行拆掉。"""
    from app.ai.parser import clean_tikz

    code = r"\begin{tikzpicture}\node[left] at (0,0) {$A$};\end{tikzpicture}"
    assert clean_tikz(code) == code


def test_clean_tikz_glues_split_coordinate_name():
    from app.ai.parser import clean_tikz

    code = r"\begin{tikzpicture}\coordinate (A1) at (0,0);\draw (A 1) -- (B1);\end{tikzpicture}"
    out = clean_tikz(code)
    assert "(A 1)" not in out
    assert "\\draw (A1)" in out


def test_clean_tikz_leaves_real_spaced_names_alone():
    """文中没有无空格写法时，不能擅自猜测合并。"""
    from app.ai.parser import clean_tikz

    code = r"\begin{tikzpicture}\draw (A 1) -- (0, 0);\end{tikzpicture}"
    assert "(A 1)" in clean_tikz(code)


def test_render_reports_tex_error_detail():
    """编译失败要能说清是哪个坐标/命令出错，而不是只甩英文提示。"""
    with pytest.raises(tikz.TikzError) as exc:
        tikz.render(r"\begin{tikzpicture}\draw (A) -- (B 1);\end{tikzpicture}", use_cache=False)
    assert "No shape named" in str(exc.value) or "Undefined" in str(exc.value)


def _parsed(payload: dict):
    import json

    from app.ai import parser as out_parser

    return out_parser.parse(json.dumps(payload, ensure_ascii=False))


def test_compile_figure_fills_svg_and_clears_needs_figure():
    """解析结果里的 TikZ 会被编译成 SVG，同时取消「贴原图」。"""
    from app.ai import client as ai_client

    parsed = _parsed({"question_text": "如图，求面积。", "figure_tikz": TRIANGLE, "needs_figure": True})
    ai_client.compile_figure(parsed)
    assert "<svg" in (parsed.data.get("figure_svg") or "")
    assert parsed.data["needs_figure"] is False
    assert not parsed.warnings


def test_compile_figure_falls_back_when_code_is_broken():
    """编译失败不改解析结果本身，只把图降级成「贴原图」并留一条提示。"""
    from app.ai import client as ai_client

    parsed = _parsed(
        {
            "question_text": "如图，求面积。",
            "figure_tikz": r"\begin{tikzpicture}\draw (0,0)--;\end{tikzpicture}",
        }
    )
    ai_client.compile_figure(parsed)
    assert parsed.data["needs_figure"] is True
    assert not parsed.data["figure_svg"]
    assert any("题图" in w for w in parsed.warnings)


def test_calc_library_is_available():
    r"""定理图里「E 在 A1D 上」这类取点要靠 calc 计算，引擎必须支持。

    以前提示词里写"不要用 \usetikzlibrary，wasm 没装库"，导致模型只能凭感觉
    给坐标、点飘到线外。实测 calc / intersections / arrows.meta / patterns /
    decorations 都能用，只有 pgfplots 的 fillbetween 缺文件。
    """
    code = (
        "\\begin{tikzpicture}\n"
        "\\usetikzlibrary{calc}\n"
        "\\coordinate (A) at (0,0); \\coordinate (B) at (2,2);\n"
        "\\coordinate (M) at ($(A)!0.5!(B)$);\n"
        "\\draw (A)--(B); \\fill (M) circle (1.5pt);\n"
        "\\end{tikzpicture}"
    )
    svg = tikz.render(code, use_cache=False).svg
    assert "<path" in svg
    # 中点应落在 (-43.6,-43.6) 一线，不能退化成只画那条线
    assert svg.count("<path") >= 2
