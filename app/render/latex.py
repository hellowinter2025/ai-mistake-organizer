"""公式与轻量 Markdown 处理（服务端）。

导出链路里公式必须变成"真实排版"而不是原始 LaTeX 文本：
- PDF/HTML：由 Node 调用 KaTeX 渲染成 HTML（字体 base64 内联，完全离线）；
- DOCX：LaTeX → MathML → OMML（Office 原生公式），可被 Word 正常编辑。

安全：所有文本先 HTML 转义，再套用受限的 Markdown 子集，绝不直接输出用户 HTML。
"""
from __future__ import annotations

import html
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import BASE_DIR, CACHE_DIR

KATEX_DIR = BASE_DIR / "web" / "vendor" / "katex"
KATEX_JS = KATEX_DIR / "katex.min.js"
KATEX_CSS = KATEX_DIR / "katex.min.css"
KATEX_FONT_DIR = KATEX_DIR / "fonts"
NODE_SCRIPT = BASE_DIR / "tools" / "katex_render.cjs"


@dataclass
class Segment:
    kind: str  # text | math_inline | math_block
    value: str


MATH_RE = re.compile(r"(\$\$.+?\$\$|(?<!\\)\$(?!\$).+?(?<!\\)\$)", re.S)

# 这些环境/结构即使在行内书写也应独占一行，否则行高被撑爆、基线错乱
TALL_MATH = re.compile(
    r"\\begin\{(?:[pbBvV]?matrix|cases|aligned|alignedat|array|gathered|split)\}"
    r"|\\begin\{document\}"
    r"|\\[\w]*frac\{\\frac"
)


def is_tall(latex: str) -> bool:
    return bool(TALL_MATH.search(latex or ""))


def split_segments(text: str, promote_tall: bool = True) -> list[Segment]:
    text = (text or "").replace("\r\n", "\n")
    out: list[Segment] = []
    pos = 0
    for m in MATH_RE.finditer(text):
        if m.start() > pos:
            out.append(Segment("text", text[pos : m.start()]))
        token = m.group(0)
        if token.startswith("$$"):
            out.append(Segment("math_block", token[2:-2].strip()))
        else:
            body = token[1:-1].strip()
            kind = "math_block" if (promote_tall and is_tall(body)) else "math_inline"
            out.append(Segment(kind, body))
        pos = m.end()
    if pos < len(text):
        out.append(Segment("text", text[pos:]))
    return [s for s in out if s.value or s.kind != "text"]


def _inline_md(escaped: str) -> str:
    s = escaped
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", s)
    return _mark_verdict(s)


# 判对判错是错题本里最需要一眼扫到的东西，单独上色。
# 只在正文（已转义的文本片段）里做，公式区走 KaTeX，不会被误伤。
_VERDICT = re.compile(r"(正确|错误)(?![\u4e00-\u9fff])")


def _mark_verdict(s: str) -> str:
    def repl(m: re.Match[str]) -> str:
        cls = "ok" if m.group(1) == "正确" else "bad"
        return f'<span class="{cls}">{m.group(1)}</span>'

    return _VERDICT.sub(repl, s)


def markdown_to_html(text: str, *, placeholder=lambda i: f"@@MATH{i}@@") -> tuple[str, list[dict]]:
    """把题目 Markdown 转成受限 HTML，公式替换为占位符。

    返回 (html, math_items)。math_items 供批量渲染使用。
    """
    segments = split_segments(text)
    math_items: list[dict] = []
    chunks: list[str] = []
    for seg in segments:
        if seg.kind == "text":
            chunks.append(_text_to_html(seg.value))
        else:
            idx = len(math_items)
            math_items.append({"latex": seg.value, "display": seg.kind == "math_block"})
            if seg.kind == "math_block":
                chunks.append(f'<div class="math-block">{placeholder(idx)}</div>')
            else:
                chunks.append(placeholder(idx))
    html_out = "".join(chunks)
    # 行尾空格会被 _text_to_html 的 strip 吃掉，导致 "A." 和后面的公式贴在一起
    html_out = re.sub(r"([。，、；：．.,:])(@@MATH\d+@@)", r"\1 \2", html_out)
    html_out = _paragraphise(html_out)
    return html_out.strip(), math_items


def _text_to_html(raw: str) -> str:
    """把纯文本片段转成行内 HTML（保留真实换行，不额外补 <br>）。

    这里只做最小语法：列表项、编号项、粗体/斜体/行内代码。
    关键点：片段末尾不能追加 <br>，否则每个行内公式前后都会被强制换行。
    """
    lines = raw.split("\n")
    rendered: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            rendered.append("")
            continue
        # "− $x=1$ ..." 这种「项目符号后面直接跟公式」的行，公式会被切成单独片段，
        # 文本片段里只剩一个 "-"。若不特殊处理，页面上就成了负号，语义全反。
        if stripped in ("-", "*", "+"):
            rendered.append("<span class='bullet'>·</span>")
            continue
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            rendered.append(f"<span class='bullet'>·</span> {_inline_md(html.escape(m.group(1)))}")
            continue
        m = re.match(r"^(\d+)[.、)]\s+(.*)$", stripped)
        if m:
            rendered.append(f"<span class='num'>{m.group(1)}．</span>{_inline_md(html.escape(m.group(2)))}")
            continue
        rendered.append(_inline_md(html.escape(stripped)))
    return "<br>".join(rendered)


def _paragraphise(body: str) -> str:
    body = re.sub(r"<br>\s*(<div class=\"math-block\">)", r"\1", body)
    body = re.sub(r"(</div>)\s*<br>", r"\1", body)
    return body


def render_math_batch(items: list[dict]) -> tuple[list[str], list[dict]]:
    """调用 Node + KaTeX 批量渲染，返回 (html列表, 错误列表)。"""
    if not items:
        return [], []
    payload = json.dumps({"items": items}, ensure_ascii=False)
    try:
        proc = subprocess.run(
            ["node", str(NODE_SCRIPT)],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=90,
            cwd=str(BASE_DIR),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 Node.js，无法渲染公式（PDF/预览需要）") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("公式渲染超时") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"公式渲染失败：{proc.stderr.decode('utf-8', 'ignore')[:500]}")
    try:
        result = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"公式渲染返回异常：{proc.stdout[:300]!r}") from exc
    return result.get("html", []), result.get("errors", [])


def fill_placeholders(body: str, rendered: list[str], fallback: list[str]) -> str:
    def repl(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        if idx < len(rendered) and rendered[idx]:
            return rendered[idx]
        raw = fallback[idx] if idx < len(fallback) else ""
        return f'<code class="math-error">{html.escape(raw)}</code>'

    return re.sub(r"@@MATH(\d+)@@", repl, body)


_KATEX_CSS_CACHE = CACHE_DIR / "katex-inline.css"


def katex_css_inline(force: bool = False) -> str:
    """把 KaTeX CSS 的字体引用替换成 base64，保证打印时字体不缺失。"""
    if _KATEX_CSS_CACHE.exists() and not force:
        cached = _KATEX_CSS_CACHE.read_text(encoding="utf-8")
        if len(cached) > 1000:
            return cached
    css = KATEX_CSS.read_text(encoding="utf-8")

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        font_path = KATEX_FONT_DIR / name
        if not font_path.exists():
            return "url()"
        import base64

        data = base64.b64encode(font_path.read_bytes()).decode()
        return f"url(data:font/woff2;base64,{data}) format('woff2')"

    css = re.sub(r"url\(fonts/([^)]+?\.woff2)\)\s*format\(['\"]woff2['\"]\)", repl, css)
    css = re.sub(r"url\(fonts/([^)]+?\.woff2)\)", repl, css)
    # 只内联 woff2：woff/ttf 备用源删掉，避免打印时出现 404 或体积膨胀
    css = re.sub(r",\s*url\(fonts/[^)]+\.(?:woff|ttf)\)\s*(?:format\([^)]*\))?", "", css)
    css = re.sub(r"url\(fonts/[^)]+\.(?:woff|ttf)\)\s*(?:format\([^)]*\))?", "url()", css)
    css = re.sub(r",\s*;", ";", css)
    _KATEX_CSS_CACHE.write_text(css, encoding="utf-8")
    return css


# latex2mathml 认识 align/split/gathered/matrix，**唯独不认 aligned**
# （KaTeX 里最常用的那个）。导出前把它换掉，否则整条公式退化成字面文本。
_OMML_ENV_MAP = (
    (r"\begin{alignedat}", r"\begin{split}"),
    (r"\end{alignedat}", r"\end{split}"),
    # latex2mathml 的 align 会生成带编号的公式行（右边冒出 (1)(2)），
    # 而错题里的多行推导根本不该编号，所以映射到 split（同样保留 & 对齐）。
    (r"\begin{aligned}", r"\begin{split}"),
    (r"\end{aligned}", r"\end{split}"),
    (r"\begin{gather}", r"\begin{gathered}"),
    (r"\end{gather}", r"\end{gathered}"),
    (r"\begin{align*}", r"\begin{split}"),
    (r"\end{align*}", r"\end{split}"),
)

# 填空横线：``\underline{\hspace{2cm}}`` 在 OMML 里只会剩一根短横"―"，
# 看不出是要填空。换成等宽的下划线文本，Word 里才像一条作答横线。
_FILL_BLANK = re.compile(r"\\underline\{\s*\\hspace\*?\{[^{}]*\}\s*\}")


def _omml_friendly(latex: str) -> str:
    text = _FILL_BLANK.sub(r"\\text{＿＿＿＿＿＿}", latex)
    for src, dst in _OMML_ENV_MAP:
        text = text.replace(src, dst)
    return text


def latex_to_omml(latex: str, xsl_path: Path | None = None) -> str | None:
    """LaTeX → OMML（Word 原生公式）。失败返回 None。"""
    try:
        import latex2mathml.converter as l2m
        from lxml import etree
    except ImportError:
        return None
    xsl = xsl_path or Path(r"C:\Program Files\Microsoft Office\root\Office16\MML2OMML.XSL")

    def _convert(src: str) -> str | None:
        try:
            mathml = l2m.convert(src)
        except Exception:  # noqa: BLE001 - 第三方库异常类型不稳定
            return None
        if not xsl.exists():
            return None
        try:
            dom = etree.fromstring(mathml.encode("utf-8"))
            transform = etree.XSLT(etree.parse(str(xsl)))
            result = transform(dom)
            text = str(result)
        except Exception:  # noqa: BLE001 - XSLT 失败时降级为纯文本
            return None
        return text if "oMath" in text else None

    # 先按 Word 的口味改一遍（aligned→split、填空线下划线化），失败再退回原文试
    text = _convert(_omml_friendly(latex))
    if text is None:
        friendly = _omml_friendly(latex)
        if friendly != latex:
            text = _convert(latex)
    return text


KATEX_UNSUPPORTED = {
    "\\label",
    "\\tag",
    "\\includegraphics",
    "\\begin{document}",
    "\\textcolor",
    "\\href",
    "\\newcommand",
    "\\def",
}


# ---------------------------------------------------------------- 化学式
# KaTeX 靠 mhchem 支持 \ce{}，但 DOCX 走的是 latex2mathml → OMML，
# 那边不认 \ce（实测会原样输出成字面 "\ce" + 字母）。导出前先降级成普通 LaTeX。

_CE_BLOCK = re.compile(r"\\ce\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")
_CE_ARROWS = (("<=>", r"\rightleftharpoons "), ("<->", r"\leftrightarrow "))
_CE_ARROWS += (("->", r"\rightarrow "), ("<-", r"\leftarrow "))

# 上下标 Unicode 映射，转换失败时退成纯文本用
_SUB = dict(zip("0123456789+-", "₀₁₂₃₄₅₆₇₈₉₊₋"))
_SUP = dict(zip("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻"))


def _ce_body_to_latex(body: str) -> str | None:
    r"""把 ``\ce`` 里的内容转成 latex2mathml 能吃的普通 LaTeX。

    覆盖中学常见的写法：元素加数字下标、前置系数、离子电荷、箭头、
    沉淀 ↓ / 气体 ↑、结晶水。带反应条件 ``->[...]`` 或含汉字的返回 None
    （这类交给上层退成纯文本，宁可不排公式也不能印出错的东西）。
    """
    s = (body or "").strip()
    if not s or "[" in s:
        return None
    if re.search(r"[^\x00-\x7f]", s):  # 汉字/特殊符号一律不猜
        return None

    # 先把 ^{2+} / ^+ 这类电荷藏起来，免得被下面的下标规则打散
    holds: list[str] = []

    def hold(m: re.Match[str]) -> str:
        holds.append(m.group(0))
        return f"\x00{len(holds) - 1}\x00"

    s = re.sub(r"\^\{[^{}]*\}|\^[0-9+\-]", hold, s)

    for token, cmd in _CE_ARROWS:
        s = s.replace(token, cmd)
    s = s.replace("·", r" \cdot ").replace("*", r" \cdot ")

    # 沉淀/气体：不在末尾也可能出现（CaCO3 v + 2NaCl），所以匹配"前后有界"的 v / ^
    s = re.sub(r"(?<=[0-9A-Za-z\)])\s+v(?=\s|\+|$)", r" \\downarrow ", s)
    s = re.sub(r"(?<=[0-9A-Za-z\)])\s*\^(?=\s|\+|$)", r" \\uparrow ", s)

    # 元素/右括号 + 数字 → 下标：H2SO4 → H_{2}SO_{4}，Ca(OH)2 → Ca(OH)_{2}
    s = re.sub(r"([A-Za-z\)])(\d+)", r"\1_{\2}", s)

    for i, chunk in enumerate(holds):
        s = s.replace(f"\x00{i}\x00", chunk)
    return r"\mathrm{" + s.strip() + "}"


def ce_to_latex(text: str) -> str:
    r"""把混合文本里的 ``\ce{...}`` 换成普通 LaTeX；转不了的保持原样。"""

    def repl(m: re.Match[str]) -> str:
        converted = _ce_body_to_latex(m.group(1))
        return converted if converted is not None else m.group(0)

    return _CE_BLOCK.sub(repl, text or "")


def has_ce(text: str) -> bool:
    return bool(_CE_BLOCK.search(text or ""))


def ce_to_unicode(text: str) -> str:
    r"""最后兜底：``\ce{H2SO4}`` → ``H₂SO₄``，保证 Word 里至少能读。"""

    def to_plain(body: str) -> str:
        s = (body or "").strip()
        s = s.replace("<=>", " ⇌ ").replace("->", " → ").replace("<-", " ← ")
        s = re.sub(r"(?<=[0-9A-Za-z\)])\s+v(?=\s|\+|$)", "↓", s)
        s = re.sub(r"(?<=[0-9A-Za-z\)])\s*\^(?=\s|\+|$)", "↑", s)
        s = re.sub(
            r"\^\{([0-9+\-]+)\}|\^([0-9+\-])",
            lambda m: "".join(_SUP.get(ch, ch) for ch in (m.group(1) or m.group(2))),
            s,
        )
        s = re.sub(r"(?<=[A-Za-z\)])(\d+)", lambda m: "".join(_SUB.get(ch, ch) for ch in m.group(1)), s)
        return s

    return _CE_BLOCK.sub(lambda m: to_plain(m.group(1)), text or "")


def lint_latex(latex: str) -> list[str]:
    """静态检查明显不兼容的写法，导出前给出提示。"""
    problems = []
    for token in KATEX_UNSUPPORTED:
        if token in latex:
            problems.append(f"包含不受支持的宏 {token}")
    if latex.count("{") != latex.count("}"):
        problems.append("花括号不配对")
    if re.search(r"\\begin\{(align|equation)\*?\}", latex):
        problems.append("应使用 aligned 环境")
    return problems
