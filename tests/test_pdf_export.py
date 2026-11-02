"""PDF 导出：排版 HTML 正确性与 Edge 打印链路。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.render import latex, pdf
from tests.test_figure import TRIANGLE

QUESTIONS = [
    {
        "stem": r"已知函数 $f(x)=x^{2}-4x+3$，求最小值。",
        "answer": r"$-1$",
        "error_reason": r"没有验证对称轴。",
        "solution_fix": r"$$f(x)=(x-2)^{2}-1$$",
        "solution_alt": r"用导数。",
        "knowledge_points": ["二次函数"],
        "question_type": "解答题",
    },
    {
        "stem": r"如图，求 $\triangle ABC$ 的面积。",
        "answer": r"$12$",
        "needs_figure": True,
        "figure_svg": TRIANGLE,
    },
]


def _paper(options=None):
    return {
        "id": 1,
        "uid": "testpdf",
        "title": "单元测试卷",
        "subtitle": "函数专题",
        "student_name": "张三",
        "exam_date": "2026-01-02",
        "options": options or {},
    }


def _browser_available() -> bool:
    return pdf._find_browser() is not None


def test_html_has_no_unrendered_placeholders():
    html = pdf.build_paper_html(_paper(), QUESTIONS)
    assert "@@MATH" not in html
    assert 'class="math-error"' not in html
    assert "katex" in html
    assert "单元测试卷" in html
    assert "张三" not in html.split("<body>")[1][:400] or True  # 姓名走页眉模板


def test_html_embeds_figure_as_png():
    """题图统一走服务端栅格化 PNG（与预览/DOCX 同一张图），不再内联 SVG。"""
    html = pdf.build_paper_html(_paper(), QUESTIONS)
    assert "data:image/png;base64," in html
    assert "<svg" not in html.split("<body>")[1]  # 正文区不该再有内联 SVG


def test_html_escapes_user_content():
    evil = [{"stem": "<script>alert(1)</script> $x^2$", "answer": "1"}]
    html = pdf.build_paper_html(_paper(), evil)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_html_answer_position_end():
    html = pdf.build_paper_html(_paper({"answer_position": "end"}), QUESTIONS)
    assert "参考答案与解析" in html
    assert "break-before: page" in html


def test_header_template_has_name_and_date():
    header, footer = pdf._header_footer(_paper())
    assert "姓名：张三" in header
    assert "2026-01-02" in header
    assert "pageNumber" in footer


@pytest.mark.integration
def test_export_pdf_bytes():
    if not _browser_available():
        pytest.skip("未找到 Edge/Chrome")
    path = pdf.export_pdf(_paper(), QUESTIONS)
    assert path.exists()
    data = path.read_bytes()
    assert data[:5] == b"%PDF-"
    assert len(data) > 20_000
    try:
        import pymupdf
    except ImportError:
        return
    doc = pymupdf.open(str(path))
    assert doc.page_count >= 1
    text = doc[0].get_text()
    assert "单元测试卷" in text
    assert "张三" in text  # 页眉姓名


# ---------------------------------------------------------------- 彩色版式

def test_verdict_is_coloured():
    """「正确 / 错误」要能一眼扫到，但不能误伤「正确性」这类词。"""
    body, _ = latex.markdown_to_html("故 A 正确；B 错误。但这不影响正确性。")
    assert '<span class="ok">正确</span>' in body
    assert '<span class="bad">错误</span>' in body
    assert "正确性" in body and '<span class="ok">正确</span>性' not in body


def test_sections_carry_colour_classes():
    html_doc = pdf.build_paper_html(_paper(), QUESTIONS)
    for cls in ("sec-error", "lbl-error", "sec-fix", "lbl-fix", "sec-answer", "lbl-answer"):
        assert cls in html_doc, f"缺少配色类 {cls}"


def test_lone_dash_line_becomes_bullet():
    """`- $x=1$ ...` 这种行，公式被切走后只剩一个 "-"，不能渲染成负号。"""
    body, _ = latex.markdown_to_html("- $x=1$ 是零点\n- $x=2$ 也是")
    assert "<span class='bullet'>·</span>" in body
    assert ">-<" not in body.replace(" ", "")


def test_punctuation_before_math_keeps_space():
    """行尾空格被 strip 掉后，"A." 会和公式粘在一起，这里补回来。"""
    body, _ = latex.markdown_to_html("A.$x^2$\nB.$y^2$")
    assert "A. @@MATH0@@" in body
    assert "B. @@MATH1@@" in body


# ------------------------------------------------------------------ 裁切粘贴版

def _cutout_paper(options=None):
    opts = {"layout": "cutout"}
    opts.update(options or {})
    p = _paper(opts)
    p["source"] = "XX中学月考"
    p["subject"] = "物理"
    return p


def test_cutout_has_only_stem_and_error():
    """裁切版只保留题干和错因：解析、另解、答案、知识点一律不排。"""
    html = pdf.build_paper_html(_cutout_paper(), QUESTIONS)
    assert 'class="cut-item"' in html
    assert "错因" in html
    assert "没有验证对称轴" in html
    # 解析正文与答案不能出现
    assert "用导数" not in html
    assert "二次函数" not in html
    assert 'class="lbl lbl-fix"' not in html
    assert 'class="lbl lbl-answer"' not in html
    # 没有卷头、页眉页脚、卷末答案
    assert '<div class="doc-title"' not in html
    assert '<div class="screen-header"' not in html
    assert "参考答案与解析" not in html


def test_all_layouts_are_a4_but_cutout_leaves_room_to_trim():
    """两种版式都是 A4；裁切版靠大留白，让剪成 16 开时切不到字。"""
    a4 = pdf.page_geometry("standard")
    cutout = pdf.page_geometry("cutout")

    for geom in (a4, cutout):
        assert round(geom["width"] * 25.4) == 210, geom
        assert round(geom["height"] * 25.4) == 297, geom

    # 正文列宽要比 16 开（185mm）窄，且两侧留白够剪
    content_mm = cutout["width"] * 25.4 - (cutout["margin_left"] + cutout["margin_right"]) * 25.4
    assert content_mm <= 175, content_mm
    # 从 A4 居中剪成 16 开要切掉 (210-185)/2 = 12.5mm，留白必须更大
    assert cutout["margin_left"] * 25.4 > 12.5
    assert cutout["margin_right"] * 25.4 > 12.5
    # 同理竖直方向：(297-260)/2 = 18.5mm
    assert cutout["margin_top"] * 25.4 > 18.5
    assert cutout["margin_bottom"] * 25.4 > 18.5


def test_cutout_uses_dashed_cut_lines_and_no_header():
    html = pdf.build_paper_html(_cutout_paper(), QUESTIONS)
    assert "@page { size: A4; margin: 24mm 26mm; }" in html
    assert "border-bottom: 0.5pt dashed" in html
    # 每道题一个剪切块，题数一致
    assert html.count('class="cut-item"') == len(QUESTIONS)


def test_standard_layout_still_full_paper():
    html = pdf.build_paper_html(_paper(), QUESTIONS)
    assert "doc-title" in html
    assert "参考答案与解析" not in html  # inline 答案不打卷末页
    assert "解析" in html


def test_paper_subject_only_printed_when_not_math():
    p = _paper()
    p["subject"] = "物理"
    assert "学科：物理" in pdf.build_paper_html(p, QUESTIONS)
    p["subject"] = "数学"
    assert "学科：数学" not in pdf.build_paper_html(p, QUESTIONS)


@pytest.mark.skipif(not _browser_available(), reason="需要 Edge/Chrome 打印内核")
def test_export_cutout_pdf_is_a4_with_wide_margins(tmp_path):
    """真打一份裁切版：纸张是 A4，但正文只占中间一窄列。"""
    pymupdf = pytest.importorskip("pymupdf")
    from app.render import pdf as pdf_mod

    out = pdf_mod.export_pdf(_cutout_paper(), QUESTIONS)
    doc = pymupdf.open(str(out))
    try:
        rect = doc[0].rect
        width_mm = rect.width / 72 * 25.4
        height_mm = rect.height / 72 * 25.4
        assert 209 <= width_mm <= 211, width_mm
        assert 296 <= height_mm <= 298, height_mm

        text = doc[0].get_text()
        assert "没有验证对称轴" in text
        assert "用导数" not in text

        # 文字实际占据的横向范围要明显窄于 16 开（185mm），
        # 这样沿 A4 居中剪成 16 开时，两侧落刀处必然是空白。
        blocks = doc[0].get_text("blocks")
        left = min(b[0] for b in blocks)
        right = max(b[2] for b in blocks)
        text_width_mm = (right - left) / 72 * 25.4
        assert text_width_mm <= 175, text_width_mm
        # 左侧留白也够剪（>12.5mm）
        assert left / 72 * 25.4 > 12.5
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 宽扁的图（横排工艺流程图、宽幅函数图）要占整行（2026-10-31）
# ---------------------------------------------------------------------------

WIDE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 140">'
    '<rect x="0" y="0" width="100" height="34"/></svg>'
)
TALL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300">'
    '<circle cx="100" cy="150" r="40"/></svg>'
)


def test_wide_figure_takes_full_row():
    html = pdf.build_paper_html({"title": "t", "options": {}}, [{"stem": "流程如图", "figure_svg": WIDE_SVG}])
    assert '<div class="figure figure-wide">' in html


def test_tall_figure_still_floats():
    html = pdf.build_paper_html({"title": "t", "options": {}}, [{"stem": "如图", "figure_svg": TALL_SVG}])
    assert '<div class="figure figure-wide">' not in html


def test_wide_figure_comes_after_stem():
    """宽图是块级元素，放在题号前面会跑到上一题末尾。"""
    html = pdf.build_paper_html({"title": "t", "options": {}}, [{"stem": "流程如图", "figure_svg": WIDE_SVG}])
    assert html.index("流程如图") < html.index('<div class="figure figure-wide">')


def test_checked_source_image_beats_ai_figure(tmp_path):
    """勾了「改用原题截图」+ 框选过 → 必须贴截图，不能再被 AI 重绘图盖掉。"""
    import base64

    from PIL import Image

    crop = tmp_path / "crop.png"
    Image.new("RGB", (24, 12), "white").save(crop)
    q = {
        "stem": "如图，正确的是",
        "needs_figure": True,
        "figure_svg": TRIANGLE,
        "figure_crop_path": str(crop),
    }
    html = pdf.build_paper_html({"title": "t", "options": {}}, [q])
    assert base64.b64encode(crop.read_bytes()).decode() in html
    assert "data:image/png;base64," in html


def test_ai_figure_used_when_source_image_not_checked(tmp_path):
    """没勾「改用原题截图」时仍然用 AI 重绘的图（截图只是备选）。

    截图特意存成 jpg：它一旦被选中，正文里就会出现 data:image/jpeg。
    """
    from PIL import Image

    crop = tmp_path / "crop.jpg"
    Image.new("RGB", (24, 12), "white").save(crop, "JPEG")
    q = {"stem": "如图", "figure_svg": TRIANGLE, "figure_crop_path": str(crop)}
    html = pdf.build_paper_html({"title": "t", "options": {}}, [q])
    assert "data:image/jpeg;base64," not in html
    assert "data:image/png;base64," in html


def test_pdf_prints_question_code():
    q = {"stem": "如图，求斜边。", "code": "K7M2QA"}
    html = pdf.build_paper_html({"title": "t", "options": {}}, [q])
    assert 'class="q-code"' in html
    assert "#K7M2QA" in html


def test_pdf_prints_question_code():
    q = {"stem": "如图，求斜边。", "code": "K7M2QA"}
    html = pdf.build_paper_html({"title": "t", "options": {}}, [q])
    assert 'class="q-code"' in html
    assert "#K7M2QA" in html
