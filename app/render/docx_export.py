"""试卷导出 DOCX：Word 原生公式（OMML）+ 页眉姓名/日期。

公式链路：LaTeX → MathML（latex2mathml）→ OMML（Office 自带 MML2OMML.XSL）
→ 直接写入段落的 ``m:oMath`` 节点。这样 Word 里公式是真正可编辑的公式对象，
而不是图片，也不会因为字体缺失变成方块。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from ..config import EXPORT_DIR
from .latex import Segment, ce_to_latex, ce_to_unicode, has_ce, latex_to_omml, split_segments

BODY_FONT_FALLBACK = ["宋体", "SimSun"]
TITLE_FONT_FALLBACK = ["方正小标宋简体", "宋体", "SimSun"]


def _installed_fonts() -> set[str]:
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
        )
        names = set()
        i = 0
        while True:
            try:
                name, _value, _type = winreg.EnumValue(key, i)
            except OSError:
                break
            names.add(name.split(" (")[0].strip())
            i += 1
        winreg.CloseKey(key)
        return names
    except Exception:  # noqa: BLE001 - 非 Windows 或注册表不可读时忽略
        return set()


_FONTS_CACHE: set[str] | None = None


def pick_font(preferred: list[str]) -> str:
    global _FONTS_CACHE
    if _FONTS_CACHE is None:
        _FONTS_CACHE = _installed_fonts()
    for name in preferred:
        if name in _FONTS_CACHE or not _FONTS_CACHE:
            return name
    return preferred[-1]


def _set_font(run, font_name: str, size_pt: float, *, bold: bool = False, italic: bool = False) -> None:
    run.font.name = font_name
    run.font.size = Pt(size_pt)
    run.bold = bold
    run.italic = italic
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), font_name)


def _omml_element(latex: str):
    xml = latex_to_omml(latex)
    if not xml:
        return None
    xml = re.sub(r"^<\?xml[^>]*\?>\s*", "", xml.strip())
    try:
        return parse_xml(xml)
    except Exception:  # noqa: BLE001 - 无法解析时降级为文本
        return None


def _add_inline_math(paragraph, latex: str, font: str, size: float, logger: list[str]) -> None:
    latex = _prepare_chem(paragraph, latex, font, size)
    if latex is None:
        return
    el = _omml_element(latex)
    if el is not None:
        paragraph._p.append(el)
        return
    logger.append(latex)
    run = paragraph.add_run(f"${latex}$")
    _set_font(run, font, size)


def _prepare_chem(paragraph, latex: str, font: str, size: float) -> str | None:
    r"""DOCX 走 latex2mathml，不认 mhchem 的 ``\ce{}``。

    能降级的就降成普通 LaTeX（Word 里仍是可编辑公式）；
    带反应条件等转不了的，退成 ``H₂SO₄`` 这样的 Unicode 纯文本并直接写入段落，
    返回 None 告诉调用方别再走公式通道。
    """
    if not has_ce(latex):
        return latex
    converted = ce_to_latex(latex)
    if not has_ce(converted):
        return converted
    run = paragraph.add_run(ce_to_unicode(latex))
    _set_font(run, font, size)
    return None


def _add_display_math(doc, latex: str, font: str, size: float, logger: list[str]) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(3)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.first_line_indent = Cm(0)
    latex = _prepare_chem(p, latex, font, size)
    if latex is None:
        return
    el = _omml_element(latex)
    if el is not None:
        omath_para = OxmlElement("m:oMathPara")
        omath_para.append(el)
        p._p.append(omath_para)
        return
    logger.append(latex)
    run = p.add_run(latex)
    _set_font(run, "Cambria Math", size, italic=True)


def _add_inline_text(paragraph, text: str, font: str, size: float, *, base_bold: bool = False) -> None:
    """支持 **粗体**、`等宽` 两个最小语法。"""
    if not text:
        return
    for chunk in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not chunk:
            continue
        if chunk.startswith("**") and chunk.endswith("**") and len(chunk) > 4:
            run = paragraph.add_run(chunk[2:-2])
            _set_font(run, font, size, bold=True)
        elif chunk.startswith("`") and chunk.endswith("`") and len(chunk) > 2:
            run = paragraph.add_run(chunk[1:-1])
            _set_font(run, "Consolas", size - 0.5)
        else:
            run = paragraph.add_run(chunk)
            _set_font(run, font, size, bold=base_bold)


def _new_body_paragraph(doc, *, indent: bool = True, space_after: float = 3.0,
                        align: str = WD_ALIGN_PARAGRAPH.JUSTIFY):
    p = doc.add_paragraph()
    fmt = p.paragraph_format
    fmt.line_spacing = 1.45
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(space_after)
    fmt.first_line_indent = Cm(0.74) if indent else Cm(0)
    p.alignment = align
    return p


def add_rich_text(doc, text: str, *, font: str, size: float, indent: bool = True,
                  prefix: tuple[str, bool] | None = None,
                  color: str | None = None,
                  align: str = WD_ALIGN_PARAGRAPH.JUSTIFY) -> list[str]:
    """把含公式的 Markdown 文本写入 docx，返回渲染失败的公式列表。

    段落按需创建：整段以块级公式结尾时不会再留下空段落。
    ``color`` 只作用于段首标签（如【错误原因】），正文保持黑色便于阅读打印。
    """
    failed: list[str] = []
    state: dict[str, object] = {"p": None, "prefixed": False}

    def current():
        p = state["p"]
        if p is None:
            p = _new_body_paragraph(doc, indent=indent, align=align)
            state["p"] = p
            if prefix and not state["prefixed"]:
                run = p.add_run(prefix[0])
                _set_font(run, font, size, bold=prefix[1])
                if color:
                    run.font.color.rgb = RGBColor.from_string(color)
                state["prefixed"] = True
        return p

    def new_paragraph():
        state["p"] = None
        return current()

    for seg in split_segments(text or ""):
        if seg.kind == "text":
            parts = seg.value.split("\n")
            for idx, part in enumerate(parts):
                if idx > 0:
                    new_paragraph()
                if part.strip():
                    _add_inline_text(current(), part.strip(), font, size)
        elif seg.kind == "math_inline":
            _add_inline_math(current(), seg.value, font, size, failed)
        else:
            _add_display_math(doc, seg.value, font, size, failed)
            state["p"] = None
    return failed


def _add_field(paragraph, instr: str, font: str, size: float) -> None:
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr_el = OxmlElement("w:instrText")
    instr_el.set(qn("xml:space"), "preserve")
    instr_el.text = instr
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "1"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr_el)
    run._r.append(fld_sep)
    run._r.append(placeholder)
    run._r.append(fld_end)
    _set_font(run, font, size)


def _header_border(paragraph) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "000000")
    borders.append(bottom)
    ppr.append(borders)


# 裁切粘贴版也是 A4，只是正文收进中间 16.2cm 一列（四周留 2.2~2.4cm），
# 沿 A4 居中剪成 16 开（18.5×26cm）时，切边落不到文字上。
CUTOUT_PAGE_CM = {"width": 21.0, "height": 29.7}
CUTOUT_MARGIN_CM = (2.4, 2.4, 2.6, 2.6)  # 上 下 左 右


def _cut_rule(doc) -> None:
    """题与题之间的虚线剪切线（Word 里是段落下边框）。"""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(6)
    ppr = p._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "dashed")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "999999")
    borders.append(bottom)
    ppr.append(borders)


def _setup_page(
    doc,
    font: str,
    *,
    width_cm: float = 21.0,
    height_cm: float = 29.7,
    margins: tuple[float, float, float, float] = (2.2, 2.0, 2.0, 2.0),
    simple: bool = False,
) -> None:
    """simple=True 用于裁切版：不要页眉横线与页脚页码，只留窄边距。"""
    top, bottom, left, right = margins
    section = doc.sections[0]
    section.page_width = Cm(width_cm)
    section.page_height = Cm(height_cm)
    section.top_margin = Cm(top)
    section.bottom_margin = Cm(bottom)
    section.left_margin = Cm(left)
    section.right_margin = Cm(right)
    if simple:
        section.header_distance = Cm(0.4)
        section.footer_distance = Cm(0.4)
        return
    section.header_distance = Cm(1.1)
    section.footer_distance = Cm(1.0)

    header_p = section.header.paragraphs[0]
    header_p.paragraph_format.tab_stops.add_tab_stop(Cm(width_cm - 4.0), WD_TAB_ALIGNMENT.RIGHT)
    header_p.paragraph_format.space_after = Pt(2)
    _header_border(header_p)

    footer_p = section.footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _fit_width(path: Path, max_h_cm: float = 4.6, max_w_cm: float = 7.2) -> float:
    """按「高度优先」算图片宽度。

    三棱柱这类竖长的图若按固定宽度放，会高到把整行文字压成一小块，
    表格行高被图撑开、下方留一大片空白。按高度封顶再反算宽度才稳。
    """
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
        if w > 0 and h > 0:
            return max(3.0, min(max_w_cm, max_h_cm * w / h))
    except Exception:  # noqa: BLE001 - 读不出尺寸就退回默认宽度
        pass
    return 5.6


WIDE_FIGURE_CM = 16.2  # A4 正文宽度，宽图按整行放


def _figure_picture(q: dict[str, Any], include_image: bool) -> tuple[Path, float] | None:
    """返回 (图片路径, 建议宽度cm)；没有图返回 None。

    优先级：**勾了「改用原题截图」就贴截图**（手工框过的纯图形 → 整题截图），
    没勾才用 AI 重绘的 SVG。勾选的意图很明确：AI 画得不如原图，别让重绘图盖过去。
    """
    if include_image or q.get("needs_figure"):
        for raw_path in (q.get("figure_crop_path"), q.get("crop_path")):
            if not raw_path:
                continue
            img_path = Path(raw_path)
            if img_path.exists():
                return img_path, _fit_width(img_path, max_h_cm=5.2, max_w_cm=8.0)

    svg = q.get("figure_svg")
    if svg:
        from .svg import revive_figure_svg, svg_to_png

        # 存量坏图（style 被早期清洗剥掉、键线画不出来）用 figure_smiles 重画
        svg = revive_figure_svg(q)
        png = svg_to_png(svg, width_px=720, scale=2)
        if png is not None:
            # 宽扁的图（横排工艺流程图、宽幅函数图）整行铺开，别塞进半栏。
            # 流程图折行后宽高比会掉到 2 左右，判据里单独放宽了（figure_is_wide）
            from .svg import figure_is_wide

            if figure_is_wide(q, svg):
                return Path(png), WIDE_FIGURE_CM
            # 化学结构式常是多个分子并排，按个数放宽
            smiles = q.get("figure_smiles")
            if isinstance(smiles, dict) and len(smiles) > 1:
                return Path(png), min(11.0, 3.2 * len(smiles) + 0.3)
            return Path(png), _fit_width(Path(png))
    return None


def _add_figure(doc, q: dict[str, Any], include_image: bool) -> None:
    """插入题图：SVG 先栅格化，失败则退回原题截图。"""
    pic = _figure_picture(q, include_image)
    if pic is not None:
        path, width_cm = pic
        try:
            doc.add_picture(str(path), width=Cm(width_cm))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            return
        except Exception:  # noqa: BLE001 - 图片损坏不应中断导出
            pass
    if q.get("needs_figure"):
        note = doc.add_paragraph()
        note.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = note.add_run("（原题配图请参见试卷原件）")
        _set_font(run, "宋体", 9)


def _reset_cell(cell) -> None:
    """清空单元格自带的空段落，免得多出一个空行。"""
    for p in list(cell.paragraphs):
        cell._element.remove(p._p)


def _append_code(container, q: dict[str, Any]) -> None:
    """在题干末尾追加题目编号（6.5pt 浅灰，几乎看不见但能对上号）。

    用 Word 里最小的可读字号 + #BDBDBD：黑白打印出来是一层极淡的灰，
    不抢正文，但拿着纸来问「#K7M2QA 这题怎么回事」时能直接定位。
    """
    code = str(q.get("code") or "").strip()
    if not code:
        return
    try:
        para = container.paragraphs[-1]
    except (AttributeError, IndexError):
        return
    run = para.add_run(f" #{code}")
    _set_font(run, "Times New Roman", 6.5)
    run.font.color.rgb = RGBColor.from_string("BDBDBD")


def _add_stem_with_figure(doc, q: dict[str, Any], pic: tuple[Path, float],
                          body_font: str, size: float, prefix: tuple[str, bool]) -> list[str]:
    """题干与题图并排：左侧文字、右侧图，用无边框两列表格实现。

    Word 没有 CSS 那种流式 float（浮动图片会盖住下面的文字），
    「表格分栏」是唯一既能让文字绕在图旁边、又不会被撑出重叠的稳定做法。
    """
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT

    path, fig_w = pic
    page_text_w = 17.0          # A4 21cm 减去左右各 2cm 页边距
    fig_w = min(fig_w, 7.2)     # 图再大就挤压正文了
    text_w = page_text_w - fig_w - 0.3  # 留一点内边距
    table = doc.add_table(rows=1, cols=2)
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    left, right = table.cell(0, 0), table.cell(0, 1)
    left.width = Cm(text_w)
    right.width = Cm(fig_w)
    for col, width in zip(table.columns, (text_w, fig_w)):
        col.width = Cm(width)
    left.vertical_alignment = WD_ALIGN_VERTICAL.TOP
    right.vertical_alignment = WD_ALIGN_VERTICAL.TOP
    _reset_cell(left)
    _reset_cell(right)
    _tighten_table(table, page_text_w)

    # 窄列里用两端对齐会把「在直三棱柱」拉成「在 直 三 棱 柱」，必须左对齐
    failed = add_rich_text(
        left, q.get("stem") or "", font=body_font, size=size, indent=False,
        prefix=prefix, align=WD_ALIGN_PARAGRAPH.LEFT,
    )
    _append_code(left, q)
    p = right.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    try:
        p.add_run().add_picture(str(path), width=Cm(max(3.0, fig_w - 0.2)))
    except Exception:  # noqa: BLE001 - 图坏了不能挡住题干
        _set_font(p.add_run("（题图缺失）"), "宋体", 9)
    return failed


def _tighten_table(table, total_cm: float) -> None:
    """表格贴着左边距、宽度精确等于正文宽，否则整体会右移一截。"""
    tblPr = table._tbl.tblPr
    for tag, attrs in (
        ("w:tblW", {"w:w": str(int(total_cm * 567)), "w:type": "dxa"}),
        ("w:tblInd", {"w:w": "0", "w:type": "dxa"}),
    ):
        for old in tblPr.findall(qn(tag)):
            tblPr.remove(old)
        el = OxmlElement(tag)
        for k, v in attrs.items():
            el.set(qn(k), v)
        tblPr.insert_element_before(
            el, "w:jc", "w:tblCellSpacing", "w:tblInd", "w:tblBorders",
            "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook",
        )


def _build_cutout_docx(doc, paper, questions, body_font: str) -> Path:
    """裁切粘贴版：题干 + 错因，题间虚线，无页眉页脚。"""
    size = 10.0
    failed: list[str] = []
    for idx, q in enumerate(questions, start=1):
        failed += add_rich_text(
            doc,
            q.get("stem") or "",
            font=body_font,
            size=size,
            indent=False,
            prefix=(f"{idx}．", True),
        )
        _append_code(doc, q)
        # 贴到本子上要能重做：有裁好的纯图形或需要原图时带上图，
        # 纯文字题不贴整块截图，避免题干印两遍
        _add_figure(doc, q, bool(q.get("figure_crop_path") or q.get("needs_figure")))
        if q.get("error_reason"):
            failed += add_rich_text(
                doc,
                q["error_reason"],
                font=body_font,
                size=size - 0.5,
                prefix=("【错因】", True),
                color="9E1F16",
            )
        if idx < len(questions):
            _cut_rule(doc)

    out = EXPORT_DIR / f"paper_{paper.get('uid') or paper.get('id')}_cutout.docx"
    doc.save(str(out))
    if failed:
        print(f"[docx] {len(failed)} 条公式无法转为 Word 公式，已按文本保留：{failed[:5]}")
    return out


def build_docx(paper: dict[str, Any], questions: list[dict[str, Any]]) -> Path:
    opts = dict(paper.get("options") or {})
    show_answer = opts.get("show_answer", True)
    show_error = opts.get("show_error_reason", True)
    show_analysis = opts.get("show_analysis", True)
    answer_position = opts.get("answer_position", "inline")
    include_image = opts.get("include_source_image", False)
    answer_space = int(opts.get("answer_space") or 0)
    show_kp = opts.get("show_knowledge_points", False)
    cutout = opts.get("layout") == "cutout"

    body_font = pick_font(BODY_FONT_FALLBACK)
    title_font = pick_font(TITLE_FONT_FALLBACK)
    size = 10.5

    doc = Document()
    default_style = doc.styles["Normal"]
    default_style.font.name = body_font
    default_style.font.size = Pt(size)
    default_style.element.rPr.rFonts.set(qn("w:eastAsia"), body_font)
    if cutout:
        _setup_page(
            doc,
            body_font,
            width_cm=CUTOUT_PAGE_CM["width"],
            height_cm=CUTOUT_PAGE_CM["height"],
            margins=CUTOUT_MARGIN_CM,
            simple=True,
        )
        return _build_cutout_docx(doc, paper, questions, body_font)

    _setup_page(doc, body_font)

    section = doc.sections[0]
    header_p = section.header.paragraphs[0]
    name_run = header_p.add_run(f"姓名：{paper.get('student_name') or ''}")
    _set_font(name_run, body_font, 9)
    tab_run = header_p.add_run("\t")
    _set_font(tab_run, body_font, 9)
    date_run = header_p.add_run(paper.get("exam_date") or date.today().isoformat())
    _set_font(date_run, body_font, 9)

    footer_p = section.footer.paragraphs[0]
    pre = footer_p.add_run("第 ")
    _set_font(pre, body_font, 8.5)
    _add_field(footer_p, " PAGE ", body_font, 8.5)
    mid = footer_p.add_run(" 页 / 共 ")
    _set_font(mid, body_font, 8.5)
    _add_field(footer_p, " NUMPAGES ", body_font, 8.5)
    tail = footer_p.add_run(" 页")
    _set_font(tail, body_font, 8.5)

    # 标题
    title_p = doc.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_p.paragraph_format.space_after = Pt(2)
    t_run = title_p.add_run(paper.get("title") or "错题练习")
    _set_font(t_run, title_font, 17, bold=False)

    subtitle = (paper.get("subtitle") or "").strip()
    if subtitle:
        sub_p = doc.add_paragraph()
        sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub_p.paragraph_format.space_after = Pt(2)
        s_run = sub_p.add_run(subtitle)
        _set_font(s_run, body_font, 9.5)

    source = (paper.get("source") or "").strip()
    subject = (paper.get("subject") or "").strip()
    meta_bits = []
    if subject and subject != "数学":
        meta_bits.append(f"学科：{subject}")
    if source:
        meta_bits.append(f"来源：{source}")
    if meta_bits:
        src_p = doc.add_paragraph()
        src_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        src_p.paragraph_format.space_after = Pt(8)
        src_run = src_p.add_run("　·　".join(meta_bits))
        _set_font(src_run, body_font, 9)
        src_run.font.color.rgb = RGBColor(0x44, 0x44, 0x44)

    failed_formulas: list[str] = []
    for idx, q in enumerate(questions, start=1):
        # 有图就并排排版（左题干右图），没图照常整行写题干
        pic = _figure_picture(q, include_image)
        # 整行宽的图不参与"左文右图"分栏：塞进窄列会缩成一条看不清的细带
        if pic is not None and pic[1] < WIDE_FIGURE_CM - 0.2:
            failed_formulas += _add_stem_with_figure(
                doc, q, pic, body_font, size, (f"{idx}．", True)
            )
        else:
            failed_formulas += add_rich_text(
                doc,
                q.get("stem") or "",
                font=body_font,
                size=size,
                indent=False,
                prefix=(f"{idx}．", True),
            )
            _append_code(doc, q)
            _add_figure(doc, q, include_image)
        if show_answer and answer_position == "inline":
            if show_error and q.get("error_reason"):
                failed_formulas += add_rich_text(
                    doc, q["error_reason"], font=body_font, size=size - 0.5,
                    prefix=("【错误原因】", True), color="B3261E",
                )
            if show_analysis and q.get("solution_fix"):
                failed_formulas += add_rich_text(
                    doc, q["solution_fix"], font=body_font, size=size - 0.5,
                    prefix=("【解析】", True), color="0D5C86",
                )
            if q.get("solution_alt"):
                failed_formulas += add_rich_text(
                    doc, q["solution_alt"], font=body_font, size=size - 0.5,
                    prefix=("【另解】", True), color="0B6B52",
                )
            if q.get("answer"):
                failed_formulas += add_rich_text(
                    doc, q["answer"], font=body_font, size=size - 0.5,
                    prefix=("【答案】", True), color="263C9C",
                )
            if show_kp and q.get("knowledge_points"):
                kp = "、".join(str(k) for k in q["knowledge_points"])
                failed_formulas += add_rich_text(
                    doc, kp, font=body_font, size=size - 0.5, prefix=("【知识点】", True), color="7A5A12",
                )
        if answer_space:
            for _ in range(answer_space):
                blank = doc.add_paragraph()
                blank.paragraph_format.space_after = Pt(10)
        # 题目之间留白
        spacer = doc.add_paragraph()
        spacer.paragraph_format.space_after = Pt(4)

    if show_answer and answer_position == "end":
        doc.add_page_break()
        end_title = doc.add_paragraph()
        end_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        e_run = end_title.add_run("参考答案与解析")
        _set_font(e_run, title_font, 14)

        for idx, q in enumerate(questions, start=1):
            if q.get("answer"):
                failed_formulas += add_rich_text(
                    doc, q["answer"], font=body_font, size=size, indent=False,
                    prefix=(f"{idx}．", True),
                )

    out = EXPORT_DIR / f"paper_{paper.get('uid') or paper.get('id')}.docx"
    doc.save(str(out))
    if failed_formulas:
        print(f"[docx] {len(failed_formulas)} 条公式无法转为 Word 公式，已按文本保留：{failed_formulas[:5]}")
    return out
