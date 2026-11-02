"""DOCX 导出：Word 原生公式、页眉姓名日期、题图。"""
from __future__ import annotations

import zipfile

from lxml import etree

from app.render import docx_export
from tests.test_figure import TRIANGLE

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}

QUESTIONS = [
    {
        "stem": r"已知函数 $f(x)=x^{2}-4x+3$，求最小值。",
        "answer": r"$-1$",
        "error_reason": r"没有验证对称轴位置。",
        "solution_fix": r"$$f(x)=(x-2)^{2}-1$$",
        "solution_alt": r"",
        "knowledge_points": ["二次函数"],
        "difficulty": 2,
        "question_type": "解答题",
    },
    {
        "stem": r"如图，$\triangle ABC$ 中 $AB=5$，求面积。",
        "answer": r"$12$",
        "needs_figure": True,
        "figure_svg": TRIANGLE,
    },
]


def _paper(options=None):
    return {
        "id": 1,
        "uid": "testpaper",
        "title": "单元测试卷",
        "subtitle": "函数专题",
        "student_name": "张三",
        "exam_date": "2026-01-02",
        "options": options or {},
    }


def _document_xml(path):
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml")


def test_docx_native_equations(tmp_path):
    path = docx_export.build_docx(_paper(), QUESTIONS)
    assert path.exists() and path.stat().st_size > 5000
    root = etree.fromstring(_document_xml(path))
    omaths = root.findall(".//m:oMath", NS)
    assert len(omaths) >= 3, f"公式数过少：{len(omaths)}"
    text = "".join(t.text or "" for t in root.findall(".//w:t", NS))
    assert "$" not in text, "正文里不该残留 LaTeX 定界符"
    assert "单元测试卷" in text
    assert "函数专题" in text


def test_docx_header_and_footer(tmp_path):
    path = docx_export.build_docx(_paper(), QUESTIONS)
    with zipfile.ZipFile(path) as z:
        header = z.read("word/header1.xml").decode("utf-8")
        footer = z.read("word/footer1.xml").decode("utf-8")
    assert "姓名：张三" in header
    assert "2026-01-02" in header
    assert "PAGE" in footer and "NUMPAGES" in footer


def test_docx_figure_becomes_picture(tmp_path):
    path = docx_export.build_docx(_paper(), QUESTIONS)
    with zipfile.ZipFile(path) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    assert media, "题图应作为图片写入 docx"


def test_docx_answer_position_end(tmp_path):
    path = docx_export.build_docx(_paper({"answer_position": "end"}), QUESTIONS)
    root = etree.fromstring(_document_xml(path))
    text = "".join(t.text or "" for t in root.findall(".//w:t", NS))
    assert "参考答案与解析" in text


def test_docx_no_answer(tmp_path):
    path = docx_export.build_docx(_paper({"show_answer": False}), QUESTIONS)
    root = etree.fromstring(_document_xml(path))
    text = "".join(t.text or "" for t in root.findall(".//w:t", NS))
    assert "错误原因" not in text


def test_docx_handles_broken_formula(tmp_path):
    """latex2mathml 无法转换的写法应降级为文本，不影响整份文档。"""
    q = [{"stem": r"坏公式 $\unknownmacro{x}$ 与正常公式 $x^2$", "answer": "1"}]
    path = docx_export.build_docx(_paper(), q)
    assert path.exists()
    root = etree.fromstring(_document_xml(path))
    text = "".join(t.text or "" for t in root.findall(".//w:t", NS))
    assert "坏公式" in text


# ------------------------------------------------------------------ 裁切粘贴版

def test_cutout_docx_is_a4_with_wide_margins():
    """Word 版裁切稿也是 A4；靠 2.4~2.6cm 留白保证剪成 16 开时不切字。"""
    paper = _paper({"layout": "cutout"})
    out = docx_export.build_docx(paper, QUESTIONS)
    with zipfile.ZipFile(out) as z:
        xml = etree.fromstring(z.read("word/document.xml"))
        sect = xml.find(".//w:sectPr", NS)
        width = int(sect.find("w:pgSz", NS).get("{%s}w" % NS["w"]))
        height = int(sect.find("w:pgSz", NS).get("{%s}h" % NS["w"]))
        # twips: 1cm = 567
        assert abs(width / 567 - 21.0) < 0.05, width
        assert abs(height / 567 - 29.7) < 0.05, height

        mar = sect.find("w:pgMar", NS)
        left_cm = int(mar.get("{%s}left" % NS["w"])) / 567
        right_cm = int(mar.get("{%s}right" % NS["w"])) / 567
        top_cm = int(mar.get("{%s}top" % NS["w"])) / 567
        # 居中剪成 16 开：左右各切 1.25cm、上下各切 1.85cm
        assert left_cm > 1.25 and right_cm > 1.25, (left_cm, right_cm)
        assert top_cm > 1.85, top_cm
        # 没有 PAGE / NUMPAGES 域
        footer = z.read("word/footer1.xml").decode("utf-8") if "word/footer1.xml" in z.namelist() else ""
        assert "PAGE" not in footer


def test_cutout_docx_has_only_stem_and_error():
    paper = _paper({"layout": "cutout"})
    out = docx_export.build_docx(paper, QUESTIONS)
    with zipfile.ZipFile(out) as z:
        text = z.read("word/document.xml").decode("utf-8")
    assert "没有验证对称轴位置" in text
    assert "【错因】" in text
    # 解析/答案/知识点都不该出现
    assert "【解析】" not in text
    assert "【答案】" not in text
    assert "二次函数" not in text
    # 题间有虚线剪切线
    assert 'w:val="dashed"' in text


def test_wide_figure_is_not_squeezed_into_side_column():
    """横排流程图放进"左文右图"的窄列会缩成看不清的细带，要整行放。"""
    from app.render.docx_export import WIDE_FIGURE_CM, _figure_picture

    wide = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 140">'
            '<rect x="0" y="0" width="100" height="34"/></svg>')
    pic = _figure_picture({"figure_svg": wide}, False)
    assert pic is not None
    assert pic[1] == WIDE_FIGURE_CM


def test_docx_prefers_crop_when_source_image_checked(tmp_path):
    """勾了「改用原题截图」+ 框选过 → 用裁剪图，别用 AI 重绘的图。"""
    from PIL import Image

    from app.render.docx_export import _figure_picture

    crop = tmp_path / "crop.png"
    Image.new("RGB", (40, 20), "white").save(crop)
    q = {"needs_figure": True, "figure_svg": "<svg/>", "figure_crop_path": str(crop)}
    pic = _figure_picture(q, False)
    assert pic is not None and str(pic[0]) == str(crop)


def test_docx_uses_ai_figure_when_not_checked(tmp_path):
    from PIL import Image

    from app.render.docx_export import _figure_picture

    crop = tmp_path / "crop.png"
    Image.new("RGB", (40, 20), "white").save(crop)
    q = {"figure_crop_path": str(crop)}  # 没有 figure_svg、也没勾选 → 什么都不贴
    assert _figure_picture(q, False) is None


def test_docx_prints_question_code(tmp_path):
    """题干末尾要带极小的编号，方便拿着纸报错定位。"""
    qs = [dict(QUESTIONS[0], code="K7M2QA")]
    path = docx_export.build_docx(_paper(), qs)
    root = etree.fromstring(_document_xml(path))
    text = "".join(t.text or "" for t in root.findall(".//w:t", NS))
    assert "#K7M2QA" in text
    # 编号必须是"很不起眼"的小字浅灰
    runs = [r for r in root.findall(".//w:r", NS) if "K7M2QA" in "".join(t.text or "" for t in r.findall("w:t", NS))]
    assert runs, "编号应作为独立 run 写入"
    rpr = runs[0].find("w:rPr", NS)
    assert rpr.find("w:color", NS).get(f"{{{NS['w']}}}val").upper() == "BDBDBD"
    assert int(rpr.find("w:sz", NS).get(f"{{{NS['w']}}}val")) <= 13  # 半磅制：<= 6.5pt


def test_docx_prints_question_code(tmp_path):
    """题干末尾要带极小的编号，方便拿着纸报错定位。"""
    qs = [dict(QUESTIONS[0], code="K7M2QA")]
    path = docx_export.build_docx(_paper(), qs)
    root = etree.fromstring(_document_xml(path))
    text = "".join(t.text or "" for t in root.findall(".//w:t", NS))
    assert "#K7M2QA" in text
    # 编号必须是"很不起眼"的小字浅灰
    runs = [r for r in root.findall(".//w:r", NS) if "K7M2QA" in "".join(t.text or "" for t in r.findall("w:t", NS))]
    assert runs, "编号应作为独立 run 写入"
    rpr = runs[0].find("w:rPr", NS)
    assert rpr.find("w:color", NS).get(f"{{{NS['w']}}}val").upper() == "BDBDBD"
    assert int(rpr.find("w:sz", NS).get(f"{{{NS['w']}}}val")) <= 13  # 半磅制：<= 6.5pt
