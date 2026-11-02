# -*- coding: utf-8 -*-
"""渲染器体检：把库里的公式 + 一批刁钻样例过一遍所有渲染通道。

检查的通道：
1. 服务端 KaTeX（PDF/网页预览用的那一套，``app.render.latex.render_math_batch``）
2. DOCX 的 OMML（``latex_to_omml``，Word 里能不能变成可编辑公式）
3. 化学 ``\\ce{}``（mhchem 通道 → DOCX 降级）
4. TikZ / RDKit 只报可用性，具体由各自单测覆盖

判定「渲染错」的两类：
- 硬失败：KaTeX 抛错 / 输出带 ``katex-error`` / OMML 返回 None
- 软失败：命令名被当成正文显示出来（``\\text`` 丢了反斜杠 → 页面上出现 "text"）

用法：
    .venv/Scripts/python.exe tools/render_audit.py            # 只查库
    .venv/Scripts/python.exe tools/render_audit.py --samples   # 连带内置样例
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.render.latex import (  # noqa: E402
    ce_to_latex,
    has_ce,
    latex_to_omml,
    render_math_batch,
    split_segments,
)

TEXT_COLUMNS = ("stem", "answer", "error_reason", "solution_fix", "solution_alt", "notes")

# 这些词一出现在可见文本里，基本就是命令名被当正文了
COMMAND_WORDS = (
    "mathrm mathbf mathit mathbb mathcal mathfrak operatorname text textbf textit "
    "frac dfrac tfrac sqrt vec overline underline bar hat tilde "
    "perp parallel angle triangle circ odot subset subseteq cdot times div pm mp "
    "le leq ge geq neq approx equiv sim cong in notin ni "
    "alpha beta gamma delta theta lambda mu sigma omega pi varphi "
    "left right quad qquad log ln lg sin cos tan cot lim sum prod int partial nabla "
    "infty ldots cdots dots begin end aligned cases matrix"
).split()

SAMPLES = [
    r"\text{平面 } AMC \perp \text{平面 } A'MC'",
    r"BD=\frac{1}{2}AC=\frac{\sqrt{2}}{2}",
    r"\sqrt{2}AB=\sqrt{2}BC",
    r"\angle ABC=90^\circ",
    r"C_1 \perp AC",
    r"A'C \parallel C'M",
    r"\vec{a}+\vec{b}=(-1,-3)",
    r"|\vec{a}+\vec{b}|=3\sqrt{2}",
    r"\overline{A_1B}\perp \overline{CD}",
    r"V=\frac{1}{3}S_{\triangle ABC}\cdot h",
    r"\begin{aligned}a&=1\\b&=2\end{aligned}",
    r"\begin{cases}x>0\\y<1\end{cases}",
    r"\int_{0}^{1}x\mathrm{d}x",
    r"\sum_{n=1}^{\infty}\frac{1}{n^2}=\frac{\pi^2}{6}",
    r"\binom{n}{k}=\frac{n!}{k!(n-k)!}",
    r"\mathrm{e}^x-m-\ln(x+m)\ge 0",
    r"\odot O",
    r"x\in A \cap B",
    r"\ce{H2SO4}",
    r"\ce{2H2 + O2 -> 2H2O}",
    r"\ce{Ca^{2+} + CO3^{2-} -> CaCO3 v}",
    r"\ce{CH3COOCH2CH3}",
]


def _visible_text(html: str) -> str:
    """KaTeX 输出的可见文字（去掉标签与不可见字符）。"""
    text = re.sub(r"<[^>]+>", "", html or "")
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, char)
    return text.replace("\u00a0", " ").replace("\u200b", "").strip()


def _text_block_words(latex: str) -> set[str]:
    """``\text{...}`` 里本就该出现的词，不算泄漏。"""
    return set(re.findall(r"[A-Za-z]{2,}", " ".join(re.findall(r"\\text\w*\s*\{([^{}]*)\}", latex))))


def _leaked_words(latex: str, visible: str) -> list[str]:
    allowed = _text_block_words(latex)
    leaked = []
    for word in re.findall(r"[A-Za-z]{2,}", visible):
        if word in allowed:
            continue
        # 源码里本来就写成了 \ln / \cos 这种命令，渲染成正体是**正确**的
        if re.search(r"\\" + word + r"\b", latex):
            continue
        if word in COMMAND_WORDS:
            leaked.append(word)
    return leaked


def collect() -> list[tuple[str, str, str]]:
    """[(来源, kind, latex)]"""
    items: list[tuple[str, str, str]] = []
    rows = db.query("select id, " + ", ".join(TEXT_COLUMNS) + " from questions where deleted=0")
    for row in rows:
        for col in TEXT_COLUMNS:
            for seg in split_segments(row[col] or ""):
                if seg.kind != "text" and seg.value.strip():
                    items.append((f"q{row['id']}.{col}", seg.kind, seg.value))
    return items


def main(argv: list[str]) -> int:
    items = collect()
    if "--samples" in argv:
        items += [("sample", "math_inline", s) for s in SAMPLES]
    print(f"公式段：{len(items)}（去重 {len({i[2] for i in items})}）")

    html, errors = render_math_batch(
        [{"latex": latex, "display": kind == "math_block"} for _, kind, latex in items]
    )
    err_map = {}
    for err in errors:
        key = err.get("latex") if isinstance(err, dict) else str(err)
        err_map[key] = err

    hard, soft = [], []
    for (src, kind, latex), out in zip(items, html):
        if latex in err_map:
            hard.append((src, latex, str(err_map[latex])[:120]))
            continue
        if "katex-error" in (out or ""):
            hard.append((src, latex, "输出含 katex-error"))
            continue
        leaked = _leaked_words(latex, _visible_text(out))
        if leaked:
            soft.append((src, latex, leaked, _visible_text(out)[:60]))

    omml_fail = []
    for src, kind, latex in items:
        if latex_to_omml(latex) is None:
            omml_fail.append((src, latex[:70]))

    ce_items = [(s, l) for s, _, l in items if has_ce(l)]
    ce_fail = [(s, l[:60]) for s, l in ce_items if ce_to_latex(l) is None]

    print(f"\n[服务端 KaTeX] 硬失败 {len(hard)}")
    for row in hard[:10]:
        print("   ", row[0], "|", row[1][:60], "|", row[2])
    print(f"[服务端 KaTeX] 命令名泄漏 {len(soft)}")
    for row in soft[:10]:
        print("   ", row[0], "|", row[1][:60], "| 泄漏", row[2], "| 可见:", row[3])
    print(f"[DOCX OMML] 转换失败 {len(omml_fail)}")
    for row in omml_fail[:10]:
        print("   ", row[0], "|", row[1])
    print(f"[化学 \\ce] {len(ce_items)} 段，无法降级 {len(ce_fail)}")
    for row in ce_fail[:10]:
        print("   ", row[0], "|", row[1])

    bad = len(hard) + len(soft) + len(omml_fail) + len(ce_fail)
    print("\n结论：" + ("全部通道正常" if not bad else f"发现 {bad} 处问题"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
