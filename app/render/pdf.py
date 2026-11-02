"""试卷导出：HTML 排版 → Edge 无头打印 PDF。

为什么用浏览器内核打印：数学公式排版（KaTeX）只有浏览器能 1:1 还原，
生成 PDF 与界面所见完全一致；同时借 DevTools 协议的 headerTemplate
实现"每页左上角姓名、右上角日期"和页脚页码。
"""
from __future__ import annotations

import base64
import html
import json
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, EXPORT_DIR
from .latex import fill_placeholders, katex_css_inline, markdown_to_html, render_math_batch

PRINT_SCRIPT = BASE_DIR / "tools" / "print_pdf.cjs"

PAPER_CSS = r"""
:root { color-scheme: light; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: "Times New Roman", "SimSun", "宋体", serif;
  font-size: 10.5pt;
  line-height: 1.72;
  color: #000;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
.doc-title {
  text-align: center;
  font-family: "SimHei", "黑体", "Microsoft YaHei", sans-serif;
  font-size: 17pt;
  font-weight: 700;
  letter-spacing: 2px;
  margin: 0 0 2mm;
}
.doc-sub {
  text-align: center;
  font-size: 10pt;
  color: #333;
  margin-bottom: 5mm;
}
.doc-source {
  text-align: center;
  font-size: 9pt;
  color: #444;
  margin: -2.5mm 0 4.5mm;
}
.doc-meta {
  display: flex;
  justify-content: space-between;
  font-size: 9.5pt;
  border-bottom: 0.6pt solid #000;
  padding-bottom: 1.5mm;
  margin-bottom: 4mm;
}
.screen-header {
  display: flex; justify-content: space-between;
  font-size: 9.5pt; border-bottom: 0.6pt solid #000;
  padding-bottom: 1.5mm; margin: 0 0 4mm;
}
@media print { .screen-header { display: none !important; } }
/* 题干整块不要拆，但解析很长时必须允许跨页，否则整题被挤到下一页、前面留白页 */
.q { margin: 0 0 4.2mm; break-inside: auto; orphans: 3; widows: 3; }
.q-line { display: flex; align-items: flex-start; break-inside: avoid; }
.q-no { flex: 0 0 auto; font-weight: 700; margin-right: 1.5mm; min-width: 6mm; }
.q-body { flex: 1 1 auto; text-align: justify; }
.q-body img { max-width: 100%; }
/* 题图靠右浮动，题干与选项自动绕排到左侧空白处；图整体封顶 34% 宽、44mm 高，
   放不下就缩，绝不独占一整行。绝对定位/固定宽度在题目长短不一的情况下必然浪费版面。 */
.q .figure {
  float: right;
  width: 34%;
  max-width: 58mm;
  margin: 0 0 1.5mm 4mm;
  text-align: center;
}
.q .figure svg,
.q .figure img {
  width: 100%;
  max-width: 100%;
  height: auto;
  max-height: 44mm;
  object-fit: contain;
}
/* 宽扁的图（横排工艺流程图、宽幅函数图）独占整行：压进 34% 的半栏后
   会缩成一条看不清的小带子，这里用更高特异性盖掉浮动与 44mm 高度限制。 */
.q .figure.figure-wide {
  float: none;
  width: 100%;
  max-width: 100%;
  margin: 2mm 0;
}
.q .figure.figure-wide svg,
.q .figure.figure-wide img { max-height: 62mm; }
/* 浮动必须在本题目内收干净，否则下一题会往图的右边挤 */
.q::after { content: ""; display: block; clear: both; }
/* 「请参见试卷原件」这类纯文字提示不参与浮动 */
.q .figure-note { float: none; margin: 1mm 0; text-align: left; }
.figure { margin: 2.5mm 0 1mm; text-align: center; }
.figure svg { max-width: 50%; height: auto; max-height: 55mm; }
.figure img { max-width: 78%; }
.figure-note { font-size: 9pt; color: #555; margin: 1mm 0; }
.tags { font-size: 8.5pt; color: #5a6472; }
/* 题目编号：印得极淡，只为「报错时能对上号」。浅灰在黑白打印机上也几乎看不见 */
.q-code { font-size: 6.6pt; color: #bdbdbd; letter-spacing: .06em; margin-left: .5em; white-space: nowrap; }
.analysis {
  margin: 1.6mm 0 0 7.5mm;
  font-size: 10pt;
}
/* 打印稿只用彩色文字区分板块，不用色块：色块在黑白打印机上会变成一片灰，
   反而压低正文可读性。这里每个 .sec 只是一层缩进，颜色落在标签文字上。 */
.sec { margin: 1.5mm 0 0; }
.lbl {
  display: inline-block;
  font-family: "SimHei", "黑体", sans-serif;
  font-size: 9pt;
  margin: 0.6mm 0 0.3mm;
  letter-spacing: 0.6px;
  font-weight: 700;
}
.lbl::after { content: "："; }
.lbl-error { color: #9e1f16; }
.lbl-fix { color: #0b5f8a; }
.lbl-alt { color: #0b6b52; }
.lbl-answer { color: #263c9c; }
.lbl-kp { color: #7a5a12; }
.ok { color: #0b7a3e; font-weight: 700; }
.bad { color: #b3261e; font-weight: 700; }
.answer { font-weight: 600; color: #263c9c; }
.workspace { border-bottom: 0.4pt dashed #bbb; margin: 2mm 0 0 7.5mm; }
.math-block { margin: 2mm 0; text-align: center; }
.math-error {
  font-family: Consolas, monospace;
  font-size: 9pt;
  color: #b00;
  padding: 0 2px;
  text-decoration: underline wavy #d88;
}
.katex { font-size: 1.06em; }
.katex-display { margin: 2.2mm 0; }
.katex-display > .katex { font-size: 1.1em; }
code { font-family: Consolas, "Courier New", monospace; font-size: 9.5pt; background: #f3f3f3; padding: 0 2px; }
ul { margin: 1mm 0 1mm 6mm; padding: 0; }
li { margin: 0.4mm 0; }
.num-item { margin: 0.4mm 0; }
.endnote { font-size: 8pt; color: #777; text-align: center; margin-top: 8mm; }
"""

# 裁切粘贴版：只有题目和错因，逐题之间画虚线剪切线，剪下来直接贴笔记本
CUTOUT_CSS = r"""
@page { size: A4; margin: 24mm 26mm; }
body { font-size: 10pt; line-height: 1.6; }
.cut-item {
  break-inside: avoid;
  padding: 0 0 2.4mm;
  margin: 0 0 2.6mm;
  border-bottom: 0.5pt dashed #999;
}
.cut-item:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
.cut-item .q-line { display: flex; align-items: flex-start; }
.cut-item .q-no { flex: 0 0 auto; font-weight: 700; margin-right: 1.4mm; min-width: 5mm; }
.cut-item .q-body { flex: 1 1 auto; text-align: justify; }
.cut-error { margin: 1.2mm 0 0 6.4mm; font-size: 9.5pt; }
.cut-error .lbl { font-size: 8.5pt; margin: 0; }
.cut-item .figure { margin: 1.6mm 0 0.6mm; }
.cut-item .figure img { max-height: 44mm; }
.cut-item .figure svg { max-height: 44mm; }
.cut-item .figure.figure-wide svg { max-height: 62mm; }
.cut-foot { font-size: 7.5pt; color: #999; text-align: center; margin-top: 3mm; }
@media screen {
  html { background: #eee; }
  /* 屏幕预览按正文列宽显示（158mm），打印时留白由 @page 负责 */
  body { background: #fff; width: 158mm; margin: 6mm auto; padding: 0;
         box-shadow: 0 1px 6px rgba(0,0,0,.18); }
}
"""


def _opts(paper: dict[str, Any]) -> dict[str, Any]:
    o = dict(paper.get("options") or {})
    o.setdefault("show_answer", True)
    o.setdefault("show_error_reason", True)
    o.setdefault("answer_position", "inline")  # inline | end
    o.setdefault("include_source_image", False)
    o.setdefault("answer_space", 0)
    o.setdefault("show_analysis", True)
    o.setdefault("show_knowledge_points", False)
    o.setdefault("layout", "standard")  # standard | cutout
    return o


# 所有导出一律 A4（210×297mm）：打印机不用改纸、不会被驱动缩放。
# 裁切粘贴版只是在 A4 上把正文收进中间 162mm 宽的一列，四周留足 22~24mm：
# 16 开是 185×260mm，从 A4 居中剪成 16 开要各切掉 12.5mm（左右）和 18.5mm（上下）；
# 这里留 26mm / 24mm，剪歪 5mm 也不会切到字。
A4_MM = {"width": 210.0, "height": 297.0}
CUTOUT_MARGIN_MM = {"top": 24.0, "bottom": 24.0, "left": 26.0, "right": 26.0}
# 正文实际可用宽度（210 - 26*2 = 158mm），比 16 开窄 27mm
CUTOUT_CONTENT_MM = A4_MM["width"] - CUTOUT_MARGIN_MM["left"] - CUTOUT_MARGIN_MM["right"]


def page_geometry(layout: str) -> dict[str, float]:
    """打印用纸张尺寸（英寸）。两种版式都是 A4，只有页边距不同。"""
    if layout == "cutout":
        m = CUTOUT_MARGIN_MM
        return {
            "width": A4_MM["width"] / 25.4,
            "height": A4_MM["height"] / 25.4,
            "margin_top": m["top"] / 25.4,
            "margin_bottom": m["bottom"] / 25.4,
            "margin_left": m["left"] / 25.4,
            "margin_right": m["right"] / 25.4,
        }
    return {
        "width": 8.27,
        "height": 11.69,
        "margin_top": 0.72,
        "margin_bottom": 0.6,
        "margin_left": 0.62,
        "margin_right": 0.62,
    }


def _render_bodies(texts: list[str]) -> list[str]:
    """批量把若干 Markdown 文本渲染成 HTML（公式统一走 KaTeX）。"""
    bodies: list[str] = []
    all_math: list[dict] = []
    spans: list[tuple[int, int]] = []
    for text in texts:
        body, math_items = markdown_to_html(text, placeholder=lambda i, base=len(all_math): f"@@MATH{base + i}@@")
        start = len(all_math)
        all_math.extend(math_items)
        spans.append((start, len(all_math)))
        bodies.append(body)
    rendered, errors = render_math_batch(all_math) if all_math else ([], [])
    if errors:
        for err in errors:
            print(f"[pdf] 公式渲染失败 #{err.get('index')}: {err.get('message')} :: {err.get('latex')}")
    fallbacks = [item["latex"] for item in all_math]
    return [fill_placeholders(b, rendered, fallbacks) for b in bodies]


def figure_font_css(questions: list[dict[str, Any]]) -> str:
    """内嵌题图用到的 TeX 字体。

    不带的话，``cmsy7`` 里的撇号（码位正好是 ASCII '0'）会被当数字渲成 "A⁰"。
    """
    from .svg import font_css_for, svg_ratio

    svgs = [q.get("figure_svg") or "" for q in questions]
    return font_css_for([s for s in svgs if s])


def _figure_html(q: dict[str, Any], opts: dict[str, Any], max_height_mm: int = 56) -> str:
    """题图：**勾了「改用原题截图」就贴截图**，否则用 AI 重绘的图。

    优先级（勾选在 AI 之上，这是用户明确表达过的意图）：
    1. ``needs_figure``（每道题的「改用原题截图」）或整卷的 ``include_source_image``
       → 贴图：先用手工框过的纯图形 ``figure_crop_path``，没有再退整块截图；
    2. 否则用 AI 重绘的图（SVG/SMILES/流程图现场栅格化）；
    3. 都没有但标了 ``needs_figure`` → 提示参见原件。

    **统一走服务端栅格化的 PNG**（和编辑器预览、DOCX 同一张图）——
    预览/导出共用一份产物，杜绝「预览一个样、导出另一个样」。
    宽扁的图（横排工艺流程图、宽幅函数图）给 ``figure-wide``：整行铺开、不浮动。
    """
    use_original = bool(q.get("needs_figure")) or bool(opts.get("include_source_image"))
    if use_original:
        # 用户手工裁过的"纯图形"优先：只保留图，不连题干文字一起贴
        for raw_path in (q.get("figure_crop_path"), q.get("crop_path")):
            if not raw_path:
                continue
            img_path = Path(raw_path)
            if img_path.exists():
                mime = "png" if img_path.suffix.lower() == ".png" else "jpeg"
                b64 = base64.b64encode(img_path.read_bytes()).decode()
                return (f'<div class="figure"><img src="data:image/{mime};base64,{b64}" '
                        f'style="max-height:{max_height_mm}mm"></div>')

    svg = q.get("figure_svg") or ""
    if svg or q.get("figure_smiles") or q.get("figure_flow"):
        from .svg import figure_is_wide, revive_figure_svg, svg_to_png

        # 没编译过 / 存量坏图（style 被早期清洗剥掉）→ 用源字段现场编译或重画
        clean = revive_figure_svg(q)
        if clean:
            png = svg_to_png(clean, width_px=1400, scale=2)
            if png is not None:
                b64 = base64.b64encode(png.read_bytes()).decode()
                cls = "figure figure-wide" if figure_is_wide(q, clean) else "figure"
                return (
                    f'<div class="{cls}"><img src="data:image/png;base64,{b64}" '
                    f'style="max-height:{max_height_mm}mm"></div>'
                )
    if q.get("needs_figure"):
        return '<div class="figure-note">（原题配图请参见试卷原件）</div>'
    return ""


def _build_cutout_html(
    paper: dict[str, Any], questions: list[dict[str, Any]], opts: dict[str, Any]
) -> str:
    """裁切粘贴版：只有题干和错因，题与题之间留虚线剪切线。

    这个版式不做页眉页脚、不排姓名日期，页宽 178mm（比 16 开窄 7mm），
    打印出来沿线剪下就能贴进 16 开笔记本，不用再缩放。
    """
    texts: list[str] = []
    jobs: list[tuple[str, int]] = []
    for i, q in enumerate(questions):
        texts.append(q.get("stem") or "")
        jobs.append(("stem", i))
        if opts.get("show_error_reason", True) and q.get("error_reason"):
            texts.append(q["error_reason"])
            jobs.append(("error", i))
    bucket = dict(zip(jobs, _render_bodies(texts)))

    title = html.escape(paper.get("title") or "错题")
    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        f"<title>{title}</title>",
        f"<style>{katex_css_inline()}</style>",
        f"<style>{figure_font_css(questions)}</style>",
        f"<style>{PAPER_CSS}</style>",
        f"<style>{CUTOUT_CSS}</style>",
        "</head><body>",
    ]
    for i, q in enumerate(questions):
        # 裁切稿是要贴到本子上重做的：有手工裁过的纯图形、或 AI 明确要求保留原图时，
        # 把图带上；纯文字题不贴整块截图，否则等于把题干印两遍。
        want_figure = bool(q.get("figure_crop_path") or q.get("needs_figure"))
        block = [
            '<div class="cut-item"><div class="q-line">',
            f'<span class="q-no">{i + 1}．</span><span class="q-body">',
            bucket.get(("stem", i), ""),
            # 裁切稿篇幅紧，图压到 40mm；整块截图当图用时别把版面撑爆
            _figure_html(q, {**opts, "include_source_image": want_figure}, max_height_mm=40),
            "</span></div>",
        ]
        err = bucket.get(("error", i), "")
        if err:
            block.append(
                f'<div class="cut-error"><span class="lbl lbl-error">错因</span>'
                f"<span>{err}</span></div>"
            )
        block.append("</div>")
        parts.append("".join(block))
    parts.append("</body></html>")
    return "".join(parts)


def build_paper_html(paper: dict[str, Any], questions: list[dict[str, Any]]) -> str:
    opts = _opts(paper)
    if opts.get("layout") == "cutout":
        return _build_cutout_html(paper, questions, opts)
    title = html.escape(paper.get("title") or "错题练习")
    subtitle = html.escape(paper.get("subtitle") or "")
    student = html.escape(paper.get("student_name") or "")
    exam_date = html.escape(paper.get("exam_date") or date.today().isoformat())

    # 收集所有需要渲染的文本，一次性交给 KaTeX，避免多次启动 Node
    jobs: list[tuple[str, int]] = []  # (kind, question index)
    texts: list[str] = []
    for i, q in enumerate(questions):
        texts.append(q.get("stem") or "")
        jobs.append(("stem", i))
        if opts["show_answer"] and opts["answer_position"] == "inline":
            if opts["show_error_reason"] and q.get("error_reason"):
                texts.append(q["error_reason"]); jobs.append(("error", i))
            if opts["show_analysis"] and q.get("solution_fix"):
                texts.append(q["solution_fix"]); jobs.append(("fix", i))
            if q.get("solution_alt"):
                texts.append(q["solution_alt"]); jobs.append(("alt", i))
            if q.get("answer"):
                texts.append(q["answer"]); jobs.append(("answer", i))
        elif opts["show_answer"] and opts["answer_position"] == "end":
            if q.get("answer"):
                texts.append(q["answer"]); jobs.append(("end_answer", i))
    rendered = _render_bodies(texts)
    bucket: dict[tuple[str, int], str] = {}
    for (kind, idx), body in zip(jobs, rendered):
        bucket[(kind, idx)] = body

    parts: list[str] = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        f"<title>{title}</title>",
        f"<style>{katex_css_inline()}</style>",
        f"<style>{figure_font_css(questions)}</style>",
        f"<style>{PAPER_CSS}</style>",
        "</head><body>",
    ]
    source = html.escape(paper.get("source") or "")
    subject = html.escape(paper.get("subject") or "")
    parts.append(f'<div class="doc-title">{title}</div>')
    if subtitle:
        parts.append(f'<div class="doc-sub">{subtitle}</div>')
    # 学科只在不是默认「数学」时印出来，避免每张卷子都多一行废话
    meta_bits = []
    if subject and subject != "数学":
        meta_bits.append(f"学科：{subject}")
    if source:
        meta_bits.append(f"来源：{source}")
    if meta_bits:
        parts.append(f'<div class="doc-source">{"　·　".join(meta_bits)}</div>')
    # 屏幕预览用的姓名/日期（打印时由页眉模板接管，这里隐藏）
    parts.append(
        f'<div class="screen-header"><span>姓名：{student}</span><span>{exam_date}</span></div>'
    )

    # 题干按题号渲染
    for i, q in enumerate(questions):
        no = i + 1
        stem = bucket.get(("stem", i), "")
        qtype = str(q.get("question_type") or "")
        # 题型本身已带括号（如「选择题（多选）」）时不要再套一层，否则印成（选择题（多选））
        if not qtype:
            tag = ""
        elif "（" in qtype or "(" in qtype:
            tag = f'<span class="tags"> {html.escape(qtype)}</span>'
        else:
            tag = f'<span class="tags">（{html.escape(qtype)}）</span>'
        # 编号跟在题干末尾，小字浅灰：不影响卷面，但打印稿上能一眼对上号
        code = str(q.get("code") or "").strip()
        code_html = f'<span class="q-code">#{html.escape(code)}</span>' if code else ""
        block = [f'<div class="q">']
        # 图放在题目块最前面并向右浮动：后面整段题干和选项都会绕到它左边，
        # 比「独立一行居中」省掉一大截纵向空间。
        # 但「请参见试卷原件」是一行纯文字提示，不能浮动；放在最前面会跑到题号
        # 上面去（看起来像上一题的尾巴），所以那种情况改放到题干后面。
        figure = _figure_html(q, opts, max_height_mm=44)
        # 只有"右浮动的小图"适合放最前面（后面的题干会绕到它左边）；
        # 纯文字提示和占整行的宽图都是块级元素，放前面会跑到题号上面去。
        inline_figure = bool(figure) and not figure.startswith('<div class="figure-note"') \
            and "figure-wide" not in figure
        note_only = not inline_figure
        if inline_figure:
            block.append(figure)
        block.append(
            f'<div class="q-line"><span class="q-no">{no}．</span>'
            f'<span class="q-body">{stem}{tag}{code_html}</span></div>'
        )
        if note_only:
            block.append(figure)
        if opts["show_answer"] and opts["answer_position"] == "inline":
            block.append('<div class="analysis">')
            if opts["show_error_reason"] and q.get("error_reason"):
                block.append(
                    f'<div class="sec sec-error"><div class="lbl lbl-error">错误原因</div>'
                    f'<div>{bucket.get(("error", i), "")}</div></div>'
                )
            if opts["show_analysis"] and q.get("solution_fix"):
                block.append(
                    f'<div class="sec sec-fix"><div class="lbl lbl-fix">解析</div>'
                    f'<div>{bucket.get(("fix", i), "")}</div></div>'
                )
            if q.get("solution_alt"):
                block.append(
                    f'<div class="sec sec-alt"><div class="lbl lbl-alt">另解</div>'
                    f'<div>{bucket.get(("alt", i), "")}</div></div>'
                )
            if q.get("answer"):
                block.append(
                    f'<div class="sec sec-answer"><div class="lbl lbl-answer">答案</div>'
                    f'<div class="answer">{bucket.get(("answer", i), "")}</div></div>'
                )
            if opts["show_knowledge_points"] and q.get("knowledge_points"):
                kp = "、".join(html.escape(str(k)) for k in q["knowledge_points"])
                block.append(
                    f'<div class="sec sec-kp"><div class="lbl lbl-kp">知识点</div><div>{kp}</div></div>'
                )
            block.append("</div>")
        if opts["answer_space"]:
            lines = int(opts["answer_space"])
            block.append(f'<div class="workspace" style="height:{lines * 8}mm"></div>')
        block.append("</div>")
        parts.append("".join(block))

    if opts["show_answer"] and opts["answer_position"] == "end":
        parts.append('<div style="break-before: page"><div class="doc-title" style="font-size:14pt">参考答案与解析</div>')
        for i, q in enumerate(questions):
            code = str(q.get("code") or "").strip()
            code_html = f'<span class="q-code">#{html.escape(code)}</span>' if code else ""
            parts.append(f'<div class="q"><div class="q-line"><span class="q-no">{i + 1}．</span>'
                         f'<span class="q-body answer">{bucket.get(("end_answer", i), "") or "略"}{code_html}</span></div></div>')
        parts.append("</div>")

    parts.append('<div class="endnote">— 试卷结束 —</div>')
    parts.append("</body></html>")
    return "".join(parts)


def _header_footer(paper: dict[str, Any]) -> tuple[str, str]:
    name = html.escape(paper.get("student_name") or "")
    dt = html.escape(paper.get("exam_date") or date.today().isoformat())
    header = (
        '<div style="width:100%;padding:0 15mm;font-family:SimSun,serif;font-size:9pt;color:#000;'
        'display:flex;justify-content:space-between;">'
        f'<span>姓名：{name}</span><span>{dt}</span></div>'
    )
    footer = (
        '<div style="width:100%;text-align:center;font-family:SimSun,serif;font-size:8.5pt;color:#444;">'
        '第 <span class="pageNumber"></span> 页 / 共 <span class="totalPages"></span> 页</div>'
    )
    return header, footer


def export_pdf(paper: dict[str, Any], questions: list[dict[str, Any]]) -> Path:
    html_doc = build_paper_html(paper, questions)
    opts = _opts(paper)
    cutout = opts.get("layout") == "cutout"
    geom = page_geometry(opts.get("layout", "standard"))
    suffix = "_cutout" if cutout else ""
    out_path = EXPORT_DIR / f"paper_{paper.get('uid') or paper.get('id')}{suffix}.pdf"
    with tempfile.TemporaryDirectory(prefix="mm_pdf_") as tmp:
        tmpdir = Path(tmp)
        html_file = tmpdir / "paper.html"
        html_file.write_text(html_doc, encoding="utf-8")
        # 裁切版不要页眉页脚（姓名/日期/页码都会占掉可剪的宽度）
        header, footer = ("", "") if cutout else _header_footer(paper)
        (tmpdir / "header.html").write_text(header, encoding="utf-8")
        (tmpdir / "footer.html").write_text(footer, encoding="utf-8")

        ok = _print_with_cdp(
            html_file, out_path, tmpdir / "header.html", tmpdir / "footer.html", geom
        )
        if not ok:
            _print_cli(html_file, out_path)
    if not out_path.exists() or out_path.stat().st_size < 2000:
        raise RuntimeError("PDF 生成失败，请检查是否安装了 Microsoft Edge")
    return out_path


def _print_with_cdp(
    html_file: Path,
    out_path: Path,
    header: Path,
    footer: Path,
    geom: dict[str, float] | None = None,
) -> bool:
    geom = geom or page_geometry("standard")
    cmd = [
        "node", str(PRINT_SCRIPT), str(html_file), str(out_path), str(header), str(footer),
        f"{geom['width']:.4f}", f"{geom['height']:.4f}",
        f"{geom['margin_top']:.4f}", f"{geom['margin_bottom']:.4f}",
        f"{geom['margin_left']:.4f}", f"{geom['margin_right']:.4f}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180, cwd=str(BASE_DIR))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        print(f"[pdf] CDP 打印失败：{proc.stderr.decode('utf-8', 'ignore')[:400]}")
        return False
    return out_path.exists()


def _find_browser() -> str | None:
    for path in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(path).exists():
            return path
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _print_cli(html_file: Path, out_path: Path) -> None:
    """兜底：命令行打印，无页眉页脚（姓名字段改由正文顶部呈现）。"""
    browser = _find_browser()
    if not browser:
        raise RuntimeError("未找到 Edge/Chrome，无法生成 PDF")
    if out_path.exists():
        out_path.unlink()
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={out_path}",
        html_file.as_uri(),
    ]
    subprocess.run(cmd, capture_output=True, timeout=180)
