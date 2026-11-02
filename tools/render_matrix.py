# -*- coding: utf-8 -*-
"""全功能样卷：把所有「生成器 + 渲染器」一次性摆到同一份卷子上。

覆盖：TikZ 几何图、pgfplots 函数图、RDKit 结构式、``\\ce{}`` 化学方程式、
多行 aligned / cases / array 表格、长文本、纯文字学科、手工裁过的原图。

用法：
    .venv/Scripts/python.exe tools/render_matrix.py            # 生成 PDF + DOCX + 裁切版
    .venv/Scripts/python.exe tools/render_matrix.py pdf        # 只出标准 PDF
产物落在 data/exports/matrix_*，并把 PDF 每页栅格化成 PNG 方便肉眼核对。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import EXPORT_DIR  # noqa: E402
from app.render import chem, docx_export, pdf  # noqa: E402
from app.render.tikz import TikzError, render as render_tikz  # noqa: E402

PROBE = ROOT / "data" / "cache" / "probe"
PROBE.mkdir(parents=True, exist_ok=True)

GEO = r"""\begin{tikzpicture}[scale=0.9,thick]
\coordinate (A) at (0,0); \coordinate (B) at (2.6,0); \coordinate (C) at (3.6,1.2);
\coordinate (A1) at (0,2.4); \coordinate (B1) at (2.6,2.4); \coordinate (C1) at (3.6,3.6);
\draw (A)--(B)--(C)--(C1)--(B1)--(A1)--cycle;
\draw (B)--(B1); \draw[dashed] (A)--(A1); \draw[dashed] (A)--(C);
\node[below left] at (A){$A$}; \node[below right] at (B){$B$}; \node[right] at (C){$C$};
\node[above left] at (A1){$A_1$}; \node[above] at (B1){$B_1$}; \node[above right] at (C1){$C_1$};
\end{tikzpicture}"""

PLOT = r"""\begin{tikzpicture}
\begin{axis}[width=6.8cm,height=4.8cm,axis lines=middle,xlabel={$x$},ylabel={$y$},
  xmin=-2.4,xmax=4.4,ymin=-2,ymax=8,samples=120,tick label style={font=\scriptsize}]
\addplot[domain=-2.2:4.2,thick] {(x-1)^2-1};
\addplot[domain=-2.2:4.2,dashed,samples=2] {-1};
\node[above left,font=\scriptsize,clip=false] at (axis cs:2.6,1.2) {$y=(x-a)^2+1-a^2$};
\end{axis}
\end{tikzpicture}"""

# 8 步，故意超过一栏宽：覆盖"自动折行 + 折线 + 支线"这条路径
FLOW = {
    "steps": [
        {"name": "辉铜矿", "note": "Cu2S、Fe2O3"},
        {"name": "焙烧", "label": "过量空气", "note": "Cu2S → CuO"},
        {"name": "酸浸", "label": "稀硫酸", "note": "CuO + 2H+"},
        {"name": "过滤"},
        {"name": "氧化", "label": "通入空气"},
        {"name": "调 pH", "label": "加 CuO"},
        {"name": "蒸发浓缩", "label": "控温 80℃"},
        {"name": "冷却结晶"},
    ],
    "side": [{"after": 4, "name": "滤渣", "label": "洗涤"}],
}

# 有机合成路线：节点就是结构式（SMILES），中间夹一步纯操作
ORGANIC = {
    "steps": [
        {"name": "苯", "smiles": "c1ccccc1"},
        {"name": "溴苯", "smiles": "Brc1ccccc1", "label": "Br2 / FeBr3"},
        {"name": "分液", "note": "除杂"},
        {"name": "苯酚", "smiles": "Oc1ccccc1", "label": "NaOH 后酸化"},
    ],
    "side": [{"after": 2, "name": "HBr", "label": "尾气"}],
}


def _tikz(code: str) -> str:
    try:
        return render_tikz(code, use_cache=False).svg
    except TikzError as exc:  # 生成失败也要能看到卷子其余部分
        print("TikZ 失败:", exc)
        return ""


def build_questions() -> list[dict]:
    chem_svg = chem.render_svg({"苯酚": "Oc1ccccc1", "乙酸乙酯": "CCOC(C)=O"}) if chem.available() else ""
    return [
        {
            "uid": "m1", "question_type": "选择题", "subject": "数学",
            "stem": "如图，在直三棱柱 $ABC-A_1B_1C_1$ 中，$\\angle ABC=90^\\circ$，"
                    "$AA_1=AB=2$，则直线 $A_1C$ 与平面 $ABB_1A_1$ 所成角的正弦值为（　　）\n"
                    "A. $\\dfrac{\\sqrt{3}}{3}$\nB. $\\dfrac{\\sqrt{6}}{3}$\n"
                    "C. $\\dfrac{1}{2}$\nD. $\\text{平面 } AMC \\perp \\text{平面 } A'MC'$",
            "answer": "B",
            "error_reason": "学生把「线面角」当成了「线线角」，直接取 $\\angle A_1CA$ 计算。",
            "solution_fix": "由 $BC\\perp AB$，$BC\\perp BB_1$ 得 $BC\\perp$ 平面 $ABB_1A_1$，"
                            "故 $\\angle BA_1C$ 即所求线面角，"
                            "$$\\sin\\angle BA_1C=\\frac{BC}{A_1C}=\\frac{2}{2\\sqrt{3}}=\\frac{\\sqrt{3}}{3}.$$",
            "solution_alt": "建系：以 $B$ 为原点，$BA$、$BC$、$BB_1$ 为 $x,y,z$ 轴。",
            "knowledge_points": ["线面角", "空间向量"],
            "figure_svg": _tikz(GEO),
            "needs_figure": False,
        },
        {
            "uid": "m2", "question_type": "填空题", "subject": "数学",
            "stem": "已知函数 $f(x)=x^2-2ax+1$ 在 $[1,+\\infty)$ 上单调递增，则实数 $a$ 的取值范围是 "
                    "$\\underline{\\hspace{2cm}}$。",
            "answer": "$a\\le 1$",
            "error_reason": "把对称轴位置与区间端点的大小关系弄反了。",
            "solution_fix": "对称轴 $x=a$，需 $a\\le 1$。函数图像如下：",
            "solution_alt": "$f'(x)=2x-2a\\ge 0$ 在 $[1,+\\infty)$ 恒成立。",
            "knowledge_points": ["二次函数", "单调性"],
            "figure_svg": _tikz(PLOT),
            "needs_figure": False,
        },
        {
            "uid": "m3", "question_type": "解答题", "subject": "化学",
            "stem": "苯酚与乙酸乙酯是常见的有机化合物。\n"
                    "(1) 写出苯酚与乙酸乙酯的结构简式；\n"
                    "(2) 写出苯酚钠溶液中通入 $\\ce{CO2}$ 的化学方程式。",
            "answer": "(1) 苯酚 $\\ce{C6H5OH}$，乙酸乙酯 $\\ce{CH3COOCH2CH3}$；\n"
                      "(2) $\\ce{C6H5ONa + CO2 + H2O -> C6H5OH + NaHCO3}$",
            "error_reason": "方程式漏写 $\\ce{NaHCO3}$ 的生成条件，把产物写成 $\\ce{Na2CO3}$。",
            "solution_fix": "由酸性 $\\ce{H2CO3 > C6H5OH > HCO3-}$ 可知产物为 $\\ce{NaHCO3}$。"
                            "离子方程式：$\\ce{C6H5O- + CO2 + H2O -> C6H5OH v + HCO3-}$",
            "solution_alt": "",
            "knowledge_points": ["苯酚", "酯", "酸性比较"],
            "figure_svg": chem_svg,
            "figure_smiles": {"苯酚": "Oc1ccccc1", "乙酸乙酯": "CCOC(C)=O"},
            "needs_figure": False,
        },
        {
            "uid": "m4", "question_type": "解答题", "subject": "数学",
            "stem": "已知数列 $\\{a_n\\}$ 满足 $a_1=1$，$a_{n+1}=2a_n+1$。\n"
                    "(1) 求 $\\{a_n\\}$ 的通项公式；(2) 求前 $n$ 项和 $S_n$。",
            "answer": "(1) $a_n=2^n-1$；(2) $S_n=2^{n+1}-n-2$",
            "error_reason": "构造等比数列时漏掉常数项，写成 $a_{n+1}+1=2(a_n+1)$ 后忘记回代。",
            "solution_fix": "两边加 $1$：\n"
                            "$$\\begin{aligned}a_{n+1}+1&=2a_n+2=2(a_n+1)\\\\\n"
                            "a_n+1&=2^{n-1}(a_1+1)=2^n\\end{aligned}$$\n"
                            "求和时分情况讨论：\n"
                            "$$S_n=\\begin{cases}2^{n+1}-n-2, & n\\ge 1\\\\ 0, & n=0\\end{cases}$$\n"
                            "对应关系如下表：\n"
                            "$$\\begin{array}{c|cccc}n & 1 & 2 & 3 & 4\\\\ \\hline a_n & 1 & 3 & 7 & 15\\end{array}$$",
            "solution_alt": "用错位相减同样可得。",
            "knowledge_points": ["数列递推", "等比数列求和"],
            "figure_svg": "",
            "needs_figure": False,
        },
        {
            "uid": "m5", "question_type": "选择题", "subject": "数学",
            "stem": "某几何体的三视图如图所示，则该几何体的体积为（　　）",
            "answer": "$\\dfrac{8}{3}$",
            "error_reason": "把俯视图的虚线忽略了，导致底面形状判断错误。",
            "solution_fix": "由三视图还原为四棱锥，底面为边长为 $2$ 的正方形，高为 $2$，"
                            "故 $V=\\dfrac{1}{3}\\times 4\\times 2=\\dfrac{8}{3}$。",
            "solution_alt": "",
            "knowledge_points": ["三视图", "体积"],
            "figure_svg": "",
            "needs_figure": True,
            "figure_crop_path": None,
        },
        {
            "uid": "m7", "question_type": "解答题", "subject": "化学",
            "stem": "以辉铜矿（主要成分 $\\ce{Cu2S}$，含少量 $\\ce{Fe2O3}$、$\\ce{SiO2}$）为原料"
                    "制备 $\\ce{CuSO4.5H2O}$ 的工艺流程如图所示。回答下列问题：\n"
                    "(1) 焙烧的目的是什么？(2) 酸浸后过滤所得滤渣的主要成分是什么？",
            "answer": "(1) 将 $\\ce{Cu2S}$ 氧化为可溶于酸的 $\\ce{CuO}$，同时除去部分硫；"
                      "(2) $\\ce{SiO2}$（及少量未反应的杂质）",
            "error_reason": "学生误以为滤渣是 $\\ce{Fe(OH)3}$，忽略了流程中没有调 pH 沉淀铁这一步。",
            "solution_fix": "由流程可知：酸浸时 $\\ce{CuO}$、$\\ce{Fe2O3}$ 溶解，"
                            "$\\ce{SiO2}$ 不与稀硫酸反应，故滤渣为 $\\ce{SiO2}$。"
                            "蒸发浓缩后冷却结晶得到胆矾。",
            "solution_alt": "",
            "knowledge_points": ["工艺流程", "铜及其化合物"],
            "figure_flow": FLOW,
            "needs_figure": False,
        },
        {
            "uid": "m8", "question_type": "解答题", "subject": "化学",
            "stem": "以苯为原料合成苯酚的路线如图所示（无机试剂任选）。"
                    "(1) 写出各步反应类型；(2) 步骤②后为什么要先分液？",
            "answer": "(1) 取代（溴代）、取代（水解）、酸化；"
                      "(2) 除去未反应的 $\\ce{Br2}$ 与生成的 $\\ce{HBr}$，避免干扰后续水解",
            "error_reason": "只写了「溴代」，没意识到水解也是取代反应；"
                            "也忽略了溴单质溶于有机物会带入下一步。",
            "solution_fix": "苯与液溴在 $\\ce{FeBr3}$ 催化下发生取代生成溴苯；"
                            "溴苯在 $\\ce{NaOH}$ 溶液中水解（取代）得苯酚钠，酸化后游离出苯酚。",
            "solution_alt": "",
            "knowledge_points": ["芳香烃", "卤代烃", "苯酚"],
            "figure_flow": ORGANIC,
            "needs_figure": False,
        },
        {
            "uid": "m6", "question_type": "简答题", "subject": "语文",
            "stem": "阅读下面的文字，完成后面题目。\n"
                    "「月光如流水一般，静静地泻在这一片叶子和花上。薄薄的青雾浮起在荷塘里。」\n"
                    "请分析这句话在表达上的妙处。",
            "answer": "运用通感与比喻，把视觉的「月光」写成可「泻」的流水，动静结合。",
            "error_reason": "只答出比喻，未指出通感，也未结合「泻」「浮」两个动词分析。",
            "solution_fix": "从修辞（比喻、通感）、炼字（泻、浮）、意境（朦胧幽静）三个层次作答。",
            "solution_alt": "",
            "knowledge_points": ["修辞手法", "炼字"],
            "figure_svg": "",
            "needs_figure": False,
        },
    ]


def main(argv: list[str]) -> int:
    wants = argv[0] if argv else "all"
    questions = build_questions()
    # 流程图要先编译成 SVG，导出层只认 figure_svg
    for q in questions:
        if q.get("figure_flow") and not q.get("figure_svg"):
            from app.render import flow as flow_mod

            q["figure_svg"] = flow_mod.render(q["figure_flow"])
    paper = {
        "uid": "matrix",
        "title": "全功能样卷",
        "subtitle": "渲染器体检",
        "source": "本地生成",
        "student_name": "测试",
        "subject": "综合",
        "options": {
            "show_answer": True,
            "show_error_reason": True,
            "show_analysis": True,
            "answer_position": "inline",
            "show_knowledge_points": True,
        },
    }
    made: list[Path] = []
    if wants in ("all", "pdf"):
        made.append(pdf.export_pdf(paper, questions))
        cut_paper = {**paper, "uid": "matrix_cut", "options": {**paper["options"], "layout": "cutout"}}
        made.append(pdf.export_pdf(cut_paper, questions))
    if wants in ("all", "docx"):
        made.append(docx_export.build_docx(paper, questions))
    for path in made:
        print("生成:", path, path.stat().st_size, "字节")

    rasterise([p for p in made if p.suffix == ".pdf"])
    return 0


def rasterise(paths: list[Path]) -> None:
    try:
        import pymupdf
    except ImportError:
        print("（没装 pymupdf，跳过栅格化）")
        return
    for path in paths:
        doc = pymupdf.open(path)
        stem = path.stem
        for i, page in enumerate(doc):
            out = PROBE / f"{stem}_p{i + 1}.png"
            page.get_pixmap(dpi=120).save(out)
            print("  页面", i + 1, "→", out.name)
        print(f"{path.name}: {doc.page_count} 页")
        doc.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
