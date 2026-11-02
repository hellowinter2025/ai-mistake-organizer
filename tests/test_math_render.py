"""公式渲染测试：与服务端导出、前端页面共用同一套 KaTeX。"""
from __future__ import annotations

import re

import pytest

from app.render import latex as L
from tests.fixtures_math import ALL, DISPLAY, INLINE


def _render(items):
    return L.render_math_batch(items)


def test_katex_available():
    assert L.KATEX_JS.exists(), "缺少 KaTeX 资源，请检查 web/vendor/katex"
    assert L.KATEX_CSS.exists()


def test_all_fixtures_render():
    """全部公式必须零错误渲染。"""
    items = [{"latex": seg.value, "display": seg.kind == "math_block"}
             for text in ALL for seg in L.split_segments(text) if seg.kind != "text"]
    assert len(items) > 40
    html, errors = _render(items)
    assert not errors, f"公式渲染失败：{errors[:3]}"
    assert len(html) == len(items)
    assert all("<span class=\"katex\"" in h or "katex-display" in h for h in html)


def test_invalid_latex_reports_error():
    html, errors = _render([{"latex": r"\frac{1}{", "display": False}])
    assert errors and "index" in errors[0]
    assert html[0] == ""


def test_trust_false_blocks_links_and_images():
    """trust=false 时不能生成可点击链接或远程图片。"""
    html, _errors = _render([{"latex": r"\href{javascript:alert(1)}{x}", "display": False}])
    assert "javascript:" not in html[0]
    assert "<a " not in html[0]


def test_display_vs_inline():
    html, _ = _render([
        {"latex": "x^2", "display": False},
        {"latex": "x^2", "display": True},
    ])
    assert "katex-display" not in html[0]
    assert "katex-display" in html[1]


def test_tall_math_promoted_to_block():
    segs = L.split_segments(r"已知 $f(x)=\begin{cases}1,&x>0\\0,&x\\le 0\end{cases}$ 求值")
    kinds = [s.kind for s in segs]
    assert "math_block" in kinds
    segs2 = L.split_segments(r"求 $x^2$ 的值")
    assert [s.kind for s in segs2].count("math_block") == 0


def test_markdown_keeps_inline_flow():
    """行内公式前后不能有 <br>，否则导出会被拆成多行。"""
    html, items = L.markdown_to_html(r"已知 $x>0$，求 $x+\frac{1}{x}$ 的最小值。")
    assert items and len(items) == 2
    assert "<br>@@MATH0@@" not in html
    assert "@@MATH0@@，" in html or "@@MATH0@@" in html
    assert "<br>" not in html


def test_newlines_preserved():
    html, _ = L.markdown_to_html("第一行\n第二行")
    assert html == "第一行<br>第二行"


def test_placeholder_fill_and_fallback():
    body = "A@@MATH0@@B@@MATH1@@C"
    out = L.fill_placeholders(body, ["<i>ok</i>", ""], ["x", r"\bad"])
    assert out == 'A<i>ok</i>B<code class="math-error">\\bad</code>C'


def test_katex_css_fonts_inlined():
    css = L.katex_css_inline()
    assert "data:font/woff2;base64," in css
    assert "url(fonts/" not in css
    assert len(css) > 100_000


def test_lint_latex_flags_unsupported():
    problems = L.lint_latex(r"\label{eq1} x^2")
    assert problems


def test_math_in_text_survives_html_escape():
    html, _ = L.markdown_to_html("<script>alert(1)</script> $x<y$")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_latex_to_omml_available():
    omml = L.latex_to_omml(r"\frac{a}{b}")
    if omml is None:
        pytest.skip("MML2OMML.XSL 不存在（未安装 Office），跳过 Word 公式测试")
    assert "oMath" in omml


@pytest.mark.parametrize("text", INLINE[:10] + DISPLAY[:5])
def test_each_fixture_roundtrip(text):
    html, items = L.markdown_to_html(text)
    rendered, errors = _render(items)
    assert not errors
    out = L.fill_placeholders(html, rendered, [i["latex"] for i in items])
    assert "@@MATH" not in out
    assert "math-error" not in out


# ---------------------------------------------------------------------------
# 多行环境与题图字体（2026-10-31）
# ---------------------------------------------------------------------------

def test_omml_handles_aligned_env():
    """latex2mathml 不认 aligned（KaTeX 最常用的那个），导出前要换成 align。"""
    src = r"\begin{aligned}a&=1\\b&=2\end{aligned}"
    assert L.latex_to_omml(src) is not None


def test_omml_friendly_env_map_only_touches_aligned():
    # 映射到 split：align 会让 Word 冒出 (1)(2) 编号，推导过程不该有编号
    assert L._omml_friendly(r"\begin{aligned}x\end{aligned}") == r"\begin{split}x\end{split}"
    # 本来就能用的不要动
    assert L._omml_friendly(r"\begin{cases}x\end{cases}") == r"\begin{cases}x\end{cases}"


def test_omml_fill_blank_becomes_underscores():
    """填空横线：OMML 只留一根短横"―"，要换成等宽下划线文本。"""
    out = L._omml_friendly(r"x=\underline{\hspace{2cm}}")
    assert "＿" in out and "hspace" not in out
    assert L.latex_to_omml(r"\underline{\hspace{2cm}}") is not None


def test_figure_fonts_are_embedded_for_pdf():
    """TikZ 的 cmsy7 里撇号码位就是 ASCII '0'，不带字体 PDF 会印成 A⁰。"""
    from app.render.svg import font_css_for

    svg = '<svg><text font-family="cmsy7">0</text><text font-family="cmmi10">A</text></svg>'
    css = font_css_for([svg])
    assert "@font-face" in css and "data:font/ttf;base64," in css
    assert "font-family:cmsy7" in css and "font-family:cmmi10" in css
    # 没引用的家族不必内嵌
    assert "cmbx10" not in css
    # 空输入不该炸
    assert font_css_for([]) == ""


def test_pdf_html_embeds_figure_fonts():
    from app.render.pdf import build_paper_html

    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
           '<text font-family="cmsy7">0</text></svg>')
    q = {"stem": "如图", "figure_svg": svg, "uid": "x"}
    html = build_paper_html({"title": "t", "options": {"show_answer": False}}, [q])
    assert "font-family:cmsy7" in html and "data:font/ttf;base64," in html
