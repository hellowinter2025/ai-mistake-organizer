"""AI 输出解析：JSON 优先，失败时退回分节文本解析，并记录降级原因。

真实世界里模型输出千奇百怪，这里做了多层兜底：
1. 严格 json.loads；
2. 去 Markdown 围栏 / 前后废话后重试；
3. 用 raw_decode 从第一个 { 开始试解析；
4. 常见破损修复：尾逗号、未转义换行、全角引号、单引号、缺失右括号；
5. 仍然失败 → 分节标记解析（[[题目]] 等）；
6. 再失败 → 中文小标题解析（"题目：" "解析："）；
7. 全部失败则把原文塞进 stem，标记 degraded，交由用户手工整理。
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any

FIELD_ALIASES = {
    "question_text": ["question_text", "question", "stem", "题目", "题干", "原题", "题目内容", "content"],
    "question_type": ["question_type", "type", "题型"],
    "answer": ["answer", "final_answer", "答案", "最终答案", "结论"],
    "error_analysis": ["error_analysis", "error_reason", "mistake", "错误原因", "错因", "错因分析", "错误分析"],
    "solution_fix": [
        "solution_fix",
        "solution",
        "corrected_solution",
        "纠正解析",
        "正确解析",
        "解析",
        "解答",
        "解题过程",
    ],
    "solution_alt": ["solution_alt", "alternative", "better_solution", "另解", "其他解法", "优解", "更优解法"],
    "knowledge_points": ["knowledge_points", "knowledge", "知识点", "考点"],
    "difficulty": ["difficulty", "难度"],
    "confidence": ["confidence", "置信度", "确信度"],
    "needs_figure": ["needs_figure", "need_figure", "需要原图", "是否含图", "含图", "keep_image"],
    # 新版让模型写 TikZ；figure_svg 只留给历史数据和模型手滑仍输出 SVG 的情况
    "figure_tikz": [
        "figure_tikz", "tikz", "tikz_code", "figure", "graph", "figure_code",
        "图形", "图形代码", "配图", "绘图代码",
    ],
    "figure_svg": ["figure_svg", "svg", "svg_code"],
    # 化学结构式：标准 SMILES，由本地 RDKit 出图（比让模型画 TikZ 可靠）
    "figure_flow": [
        "figure_flow", "flow", "flowchart", "flow_chart", "process_flow",
        "工艺流程", "流程图", "工艺流程图", "figure_process",
    ],
    "figure_smiles": [
        "figure_smiles", "smiles", "smile", "smiles_code", "smiles结构", "结构式smiles",
        "结构式", "分子结构",
    ],
    "notes": ["notes", "remark", "备注", "说明", "注意事项"],
}

# 反斜杠与命令名之间被塞进空格时，只有"后面确实是命令"才敢合并 ——
# ``\ in`` → ``\in``，但 ``\ x_2`` 必须保持 ``\ ``（LaTeX 控制空格）+ ``x_2``。
# 否则 ``\ x`` 会变成未定义命令 ``\x``，整条公式直接报错。
TEX_COMMANDS = set(
    """
    frac dfrac tfrac cfrac sqrt root binom choose over atop above
    left right big Big bigg Bigg middle
    begin end text textbf textit textrm mbox hbox operatorname mathrm mathbf
    mathit mathbb mathcal mathfrak mathsf mathtt boldsymbol bm
    quad qquad hspace vspace phantom boxed color colorbox fcolorbox
    displaystyle textstyle scriptstyle limits nolimits tag label ref eqref nonumber notag
    in notin ni subset subseteq supset supseteq cup cap setminus emptyset varnothing
    forall exists nexists neg lnot land lor implies impliedby iff
    le leq ge geq ne neq lt gt ll gg approx sim simeq cong equiv propto
    times div cdot cdots ldots dots vdots ddots pm mp ast star circ bullet
    sum prod int iint iiint oint lim limsup liminf sup inf max min arg det gcd lcm
    sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh
    ln lg log exp mod bmod pmod
    to rightarrow leftarrow Rightarrow Leftarrow leftrightarrow Leftrightarrow
    mapsto uparrow downarrow longrightarrow longleftarrow Longrightarrow
    infty partial nabla prime backprime empty prime hbar ell Re Im aleph wp
    alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa
    lambda mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon
    phi varphi chi psi omega
    Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
    angle triangle square circ perp parallel nparallel mid nmid vert Vert lvert rvert
    because therefore degree ldots cdots dots
    hat bar vec tilde dot ddot acute grave breve check widehat widetilde
    overline underline overbrace underbrace overrightarrow overleftarrow
    stackrel overset underset substack
    mathop mathrel mathbin mathord mathopen mathclose mathpunct
    hline cline multicolumn array tabular
    not rangle langle rfloor lfloor rceil lceil rbrace lbrace rbrack lbrack rvert lvert rVert lVert
    top bot rlap llap raisebox rule thinspace negthinspace scriptstyle scriptsize tiny rm bf it
    node tikz
    """
    .split()
)

# 这些是"确实要留一个空格"的命令，空格是参数分隔符而不是噪声
_SPACE_TAIL_COMMANDS = {"hspace", "vspace", "kern", "mkern", "hskip", "text", "textbf", "textit", "textrm", "mbox"}

_SPACED_CMD = re.compile(r"\\([ \t]+)([A-Za-z]{1,20})")

# 双重转义遗留：``\\mathrm{e}^x``（模型本意 ``\mathrm{e}^x``）。``\\`` 是 LaTeX 的
# 换行命令，后面紧跟正体/文本类命令没有任何语义，只会把命令名当字母渲染出来 ——
# 页面上就是凭空多出一串 "mathrm" / "sqrt" / "text"。只认"后面紧跟命令名且没有
# 空格"的形态，正常的 ``\\ ln 2`` 这类换行不受影响。
_DOUBLE_ESCAPE_CMD = re.compile(
    r"\\\\(?=(?:mathrm|mathbf|mathit|mathbb|mathcal|mathfrak|mathsf|mathtt"
    r"|boldsymbol|bm|operatorname|text|textbf|textit|textrm|mbox"
    r"|frac|dfrac|tfrac|sqrt|vec|bar|hat|tilde|overline|underline"
    r"|perp|parallel|angle|triangle|circ|odot|subset|subseteq|supset"
    r"|in|notin|ni|cdot|times|div|pm|mp|le|leq|ge|geq|neq|approx|sim|cong|equiv"
    r"|left|right|quad|qquad|log|ln|lg|sin|cos|tan|cot|sec|csc"
    r"|arcsin|arccos|arctan|lim|max|min|sum|prod|int|iint|oint"
    r"|alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|iota|kappa"
    r"|lambda|mu|nu|xi|pi|rho|sigma|tau|upsilon|phi|varphi|chi|psi|omega"
    r"|Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Phi|Psi|Omega"
    r"|partial|nabla|infty|ldots|cdots|dots|prime|degree|because|therefore)\b)"
)


def _is_tex_command(word: str) -> bool:
    if not word:
        return False
    if word in TEX_COMMANDS:
        return True
    # \in 后面可能直接跟着别的字母被切开，取最长已知前缀
    for i in range(len(word), 1, -1):
        if word[:i] in TEX_COMMANDS:
            return True
    return False


def _join_spaced_command(match: re.Match[str]) -> str:
    """``\\ in`` → ``\\in``；``\\ x`` → 原样保留（控制空格）。"""
    word = match.group(2)
    if _is_tex_command(word):
        return "\\" + word
    return match.group(0)


SECTION_MARK = re.compile(r"\[\[\s*([^\[\]]{1,20}?)\s*\]\]")
CN_HEADING = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?\s*(题目|题干|原题|题型|答案|错误原因|错因|纠正解析|正确解析|解析|解答|另解|其他解法|知识点|考点|难度|置信度|备注|说明)\s*(?:\*\*)?\s*[：:]\s*",
    re.M,
)


@dataclass
class ParsedResult:
    data: dict[str, Any]
    strategy: str
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "data": self.data,
            "strategy": self.strategy,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    # 有些模型会把整个 JSON 再包一层 ```json ... ```
    fence = re.search(r"```(?:json|JSON)?\s*(\{.*\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    return t.strip()


def _find_json_span(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


class ParseError(RuntimeError):
    """模型明明给了结构化内容，却怎么修都解析不出来。

    单独成类是为了让上层**明确报错**，而不是把一整段原始 JSON 塞进题干
    当成"识别结果"——那样用户看到的是一屏 ``\\frac`` 和 ``\\n``，
    比报错还难处理。
    """

    def __init__(self, message: str, *, raw: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.raw = raw
        self.detail = detail


# UTF-8 字节被按 cp1252/latin-1 解码的典型指纹：中文会变成 "æ–¹æ³•" 这种
_MOJIBAKE = re.compile(r"[\u00c0-\u00c3\u00e6\u00e5\u00e8][\u0080-\u00bf\u2013-\u203a]{2,}")


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(text)


def repair_mojibake(text: str) -> tuple[str, bool]:
    """尝试把"UTF-8 被当单字节编码解码"的乱码还原。

    只对**非 ASCII 片段**逐段尝试，中文段在 cp1252 下根本 encode 不出来，
    因此天然的不会误伤正常内容；还原后汉字比例必须提升才采纳。
    """
    if not _MOJIBAKE.search(text):
        return text, False
    changed = False
    pieces: list[str] = []
    for seg in re.split(r"([\x00-\x7f]+)", text):
        if not seg or seg.isascii():
            pieces.append(seg)
            continue
        fixed = None
        for codec in ("cp1252", "latin-1"):
            try:
                candidate = seg.encode(codec).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if _cjk_ratio(candidate) > _cjk_ratio(seg):
                fixed = candidate
                break
        if fixed is None:
            pieces.append(seg)
        else:
            pieces.append(fixed)
            changed = True
    return "".join(pieces), changed


_JSON_KEY_HINT = re.compile(
    r'"(?:question_text|question|stem|answer|error_analysis|solution_fix|solution_alt|'
    r'knowledge_points|difficulty|confidence|notes|figure_tikz|figure_svg|figure_smiles|figure_flow|needs_figure)"\s*:'
)

# 更宽的"JSON 指纹"：即使 key 后面的冒号/引号也被折行弄丢，也能认出这是结构化输出
_JSON_KEY_ANY = re.compile(
    r'"(?:question_text|error_analysis|solution_fix|solution_alt|needs_figure|figure_tikz|figure_svg|figure_smiles|figure_flow)"'
)


def looks_like_json(text: str) -> bool:
    """判断这段响应"本意是 JSON"，用于决定失败时是报错还是按文本兜底。"""
    t = (text or "").strip()
    if not t:
        return False
    head = t[:40].lstrip("` \t\r\n")
    if head.startswith("{") and ":" in t:
        return True
    flat = re.sub(r"\s+", "", t)
    return bool(_JSON_KEY_HINT.search(flat) or _JSON_KEY_ANY.search(flat))


# 字段头要认**所有**值类型，不能只认字符串。
# 早期写成 ``"key"\s*:\s*"``，于是 ``"knowledge_points":[...]``、``"difficulty":4``、
# ``"needs_figure":true`` 这些头都不算边界 —— 前一个字段（常常是 solution_alt）
# 会把它们连同后面整串 schema 一起吞掉，最后原样印进答案里。
_FIELD_HEAD = re.compile(r'"(?P<key>[A-Za-z_][A-Za-z_0-9]{2,31})"\s*:\s*')


def _clean_fragment(chunk: str) -> str:
    """把"字段值片段"当成 JSON 字符串内容来清理。

    已经知道它是值（不再需要判断引号边界），所以可以放心把折行、反斜杠后
    的空格、非法转义一个个修掉。
    """
    out: list[str] = []
    i = 0
    n = len(chunk)
    while i < n:
        ch = chunk[i]
        if ch in "\r\n":
            prev = _tail_char(out)
            j = i + 1
            while j < n and chunk[j] in " \t":
                j += 1
            nxt = chunk[j] if j < n else ""
            # ``\\`` 被折行劈成 ``\⏎\mathrm``：前一个反斜杠已经在 out 里了，
            # 这里再吞掉一个，否则会拼出 ``\\mathrm``（换行 + 裸字母）。
            if (
                prev == "\\"
                and nxt == "\\"
                and j + 1 < n
                and chunk[j + 1].isascii()
                and chunk[j + 1].isalpha()
            ):
                i = j + 1
                continue
            if _is_ascii_letter(prev) and _is_ascii_letter(nxt):
                out.append(" ")
            i = j
            continue
        if ch == "\\":
            nxt = chunk[i + 1] if i + 1 < n else ""
            if nxt in (" ", "\t"):
                j = i + 1
                while j < n and chunk[j] in " \t":
                    j += 1
                k = j
                while k < n and chunk[k].isascii() and chunk[k].isalpha():
                    k += 1
                word = chunk[j:k]
                after = chunk[k] if k < n else ""
                if word == "n" and (not after or not after.isascii()):
                    out.append("\n")
                    i = k
                    continue
                if word and _is_tex_command(word):
                    out.append("\\")
                    i = j
                    continue
                out.append("\\ ")
                i = j
                continue
            if nxt in ('"', "\\", "/"):
                out.append(nxt)
                i += 2
                continue
            if nxt == "n" and not (i + 2 < n and chunk[i + 2].isalpha()):
                out.append("\n")
                i += 2
                continue
            if nxt == "t" and not (i + 2 < n and chunk[i + 2].isalpha()):
                out.append("\t")
                i += 2
                continue
            out.append("\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _salvage_container(chunk: str) -> Any:
    """抢救数组 / 对象值：``["甲","乙"]``、``{"steps":[...]}``。

    整体 ``json.loads`` 优先；失败就按引号把元素抠出来（数组够用了）。
    """
    closing = "]" if chunk.lstrip()[:1] == "[" else "}"
    end = chunk.rfind(closing)
    span = chunk[: end + 1] if end >= 0 else chunk
    try:
        value = json.loads(_escape_controls(_repair_quotes(span)))
        if isinstance(value, (list, dict)):
            return value
    except (json.JSONDecodeError, ValueError):
        pass
    if closing == "}":
        return None
    items = [s.strip() for s in re.findall(r'"([^"\n]{1,120})"', span)]
    return [s for s in items if s] or None


def salvage_fields(text: str) -> dict[str, Any]:
    """从破损 JSON 里逐字段抢救。

    不依赖整体结构：用 ``"key":`` 定位每个值的起点，值延续到下一个字段头
    之前。即使某处引号失配、数字被折行劈开，也不会连累其他字段。
    只有真正解析不出来时才走这条路，尽量多救回一点可用内容。

    值按首个字符分派：``"`` 当字符串清理、``[``/``{`` 当结构抢救、
    其余（数字 / true / false / null）原样留成字符串交给下游 coerce。
    """
    heads = list(_FIELD_HEAD.finditer(text))
    if len(heads) < 2:
        return {}
    out: dict[str, Any] = {}
    for idx, m in enumerate(heads):
        canon = _canonical_key(m.group("key"))
        if not canon or canon in out:
            continue
        start = m.end()
        end = heads[idx + 1].start() if idx + 1 < len(heads) else len(text)
        chunk = text[start:end].strip()
        if not chunk:
            continue
        lead = chunk[0]
        if lead in "[{":
            value = _salvage_container(chunk)
            if value is not None:
                out[canon] = value
            continue
        if lead == '"':
            body = re.sub(r'"\s*[,\]}]?\s*$', "", chunk[1:].rstrip())
            body = re.sub(r"[,\]}\s]+$", "", body)
            value = _clean_fragment(body)
            if value:
                out[canon] = value
            continue
        # 数字 / 布尔 / null：``4``、``true``、``1``⏎``.0``（被折行劈开）
        token = re.match(r"[^,}\]]+", chunk)
        if token:
            raw_value = token.group(0).strip()
            if raw_value:
                out[canon] = raw_value
    return out


def _tail_char(chunks: list[str]) -> str:
    for chunk in reversed(chunks):
        if chunk:
            return chunk[-1]
    return ""


def _is_ascii_letter(ch: str) -> bool:
    """判断折行两侧是否都落在"英文单词"里 —— 只有这时才该补一个空格。

    下划线和**数字**刻意不算：
    - ``\\log_``⏎``a`` 必须接成 ``\\log_a``，补空格会变成 ``\\log_ a``；
    - TikZ 坐标名 ``B1`` 被折行劈成 ``B``⏎``1``，补空格就成了未定义坐标
      ``(B 1)``，编译直接报 "TeX engine render failed"（真实踩坑，2026-10-30）。
    真正需要补空格的只有 ``numer``⏎``ator`` 这种英文单词中间断行。
    """
    return bool(ch) and ch.isascii() and ch.isalpha()


def _escape_controls(s: str) -> str:
    """修复字符串内部的非法转义与裸控制字符。

    真实模型（尤其是长文本）会把输出按固定宽度**硬折行**，换行点落点完全随机：
    ``\\log_``⏎``a``、``\\\\``⏎``frac``、甚至把 JSON 的 key 劈成
    ``"solution_``⏎``alt"``。这些换行不携带任何语义，必须**整个删除**；
    早期实现把它们转成 ``\\n`` / 空格，结果 LaTeX 里凭空多出空格、
    反斜杠被吃掉，渲染直接崩掉。

    同一条扫描里还要处理：LaTeX 只写一个反斜杠（JSON 里是非法/错误转义）、
    反斜杠被换行劈开、反斜杠后多一个空格（``\\ in`` / ``\\ mathrm``）、
    字符串外的数字被折行劈开（``"confidence": 1``⏎``.0``）。
    """
    valid = set('"\\/bfnrt')
    out: list[str] = []
    i = 0
    n = len(s)
    in_str = False
    while i < n:
        ch = s[i]
        if not in_str:
            # 字符串外：数字 token 被折行劈开（1⏎.0 / 20⏎26）→ 直接粘连。
            # 这里不能动普通换行，JSON 允许字符串外任意空白。
            if ch in "\r\n":
                prev = _tail_char(out)
                j = i + 1
                while j < n and s[j] in " \t":
                    j += 1
                nxt = s[j] if j < n else ""
                if prev and prev in "0123456789." and nxt and nxt in "0123456789.":
                    i = j
                    continue
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            if ch == '"':
                in_str = True
            i += 1
            continue
        if ch == '"':
            out.append(ch)
            in_str = False
            i += 1
            continue
        # ---- 字符串内的真实换行：排版折断，直接删（两侧都是英文单词才补空格）----
        if ch in "\r\n":
            if ch == "\r" and i + 1 < n and s[i + 1] == "\n":
                i += 2
            else:
                i += 1
            prev = _tail_char(out)
            nxt = s[i] if i < n else ""
            if _is_ascii_letter(prev) and _is_ascii_letter(nxt):
                out.append(" ")
            continue
        if ch == "\t":
            out.append(" ")
            i += 1
            continue
        if ch == "\\":
            nxt = s[i + 1] if i + 1 < n else ""
            # 反斜杠紧跟换行 = 命令被折行劈开（\⏎frac），补回反斜杠并吞掉换行
            if nxt in ("\r", "\n"):
                j = i + 1
                if nxt == "\r" and j < n and s[j] == "\n":
                    j += 1
                # 注意要跳过**换行本身**：早期只跳了空格，
                # 于是 ``$\⏎\text`` 走不进下面的合并分支，第二个反斜杠又被当一次
                # 非法转义 → 输出 ``\\\\text``，解码后成了 ``\\text``（TeX 换行 +
                # 裸字母 text），页面上凭空冒出 "text"（2026-10-31 实拍）。
                while j < n and s[j] in " \t\r\n":
                    j += 1
                # 折行落在 ``\\`` 中间：``$\⏎\mathrm{e}^x``。两个反斜杠合起来才是
                # 模型本意的那**一个**（JSON 合法转义 ``\\``）。若各自补一个转义对，
                # 存进去就成了 ``\\mathrm`` —— LaTeX 里 ``\\`` 是换行，``mathrm``
                # 退化成斜体字母，页面上凭空多出一串 "mathrm"。
                if j < n and s[j] == "\\":
                    j += 1  # 另一半反斜杠，吃掉，避免被再转义一次
                out.append("\\\\")
                i = j
                continue
            if not nxt:
                out.append("\\\\")
                i += 1
                continue
            # 反斜杠后多一个空格：``\ in`` ``\ mathrm`` ``\ le`` ``\ varphi``。
            # 这是网关/模型在转义时插进来的，空格必须吞掉；但 ``\ x_2`` 这种
            # "控制空格 + 变量"要原样保留，否则会变成未定义命令 ``\x``。
            if nxt in (" ", "\t"):
                j = i + 1
                while j < n and s[j] in " \t":
                    j += 1
                k = j
                while k < n and s[k].isascii() and s[k].isalpha():
                    k += 1
                word = s[j:k]
                after = s[k] if k < n else ""
                # ``\ n`` + 非字母 = 换行被插了空格
                if word == "n" and (not after or not after.isascii()):
                    out.append("\\n")
                    i = k
                    continue
                if word and _is_tex_command(word):
                    out.append("\\\\")
                    i = j
                    continue
                # \ 后面是空格然后汉字/符号/变量：LaTeX 的强制空格，保留
                out.append("\\\\ ")
                i = j
                continue
            if nxt in valid:
                # \frac \beta \times \right 这类 LaTeX 命令在 JSON 里是"合法转义"，
                # 直接保留会被解析成控制字符（\f → 换页符），必须补成字面反斜杠。
                # 折行也可能落在命令字中间（``\t``⏎``ext``），此时后面只跟换行，
                # 同样要按命令处理，否则会解成 TAB + "ext"。
                after = s[i + 2] if i + 2 < n else ""
                if nxt in "bfnrt" and (after.isalpha() or after in ("\r", "\n")):
                    out.append("\\\\")
                    out.append(nxt)
                    i += 2
                    if after in ("\r", "\n"):
                        # 命令字被折行劈开：换行不带语义，连同缩进一起吞掉，
                        # 否则 ``\t``⏎``ext`` 会先补个空格变成 ``\t ext``。
                        while i < n and s[i] in " \t\r\n":
                            i += 1
                    continue
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2 : i + 6] or ""):
                out.append(s[i : i + 6])
                i += 6
                continue
            out.append("\\\\")
            i += 1
            continue
        if ord(ch) < 0x20:
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_QUOTE_TO_CORNER = {"\u201c": "\u300c", "\u201d": "\u300d", "\uff02": "\u300c"}


def _repair_quotes(s: str) -> str:
    """全角引号只在字符串**内部**转成「」，字符串外才当结构定界符。

    早期实现直接 ``s.replace('“', '"')``，把「三线合一」这类**引用内容**
    变成了裸双引号 —— 字符串提前闭合，整段 JSON 报 ``Expecting ',' delimiter``。
    这类响应在真实日志里出现了 3 次，是"JSON 识别失败"的主要来源之一。
    """
    out: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
                continue
            if ch == "\\":
                out.append(ch)
                esc = True
                continue
            if ch == '"':
                out.append(ch)
                in_str = False
                continue
            mapped = _QUOTE_TO_CORNER.get(ch)
            out.append(mapped if mapped else ch)
        else:
            if ch == '"':
                in_str = True
                out.append(ch)
                continue
            if ch in ("\u201c", "\uff02"):
                in_str = True
                out.append('"')
                continue
            if ch == "\u201d":
                out.append('"')
                continue
            out.append(ch)
    return "".join(out)


def _repair_json(raw: str) -> str:
    s = raw.strip()
    s = _repair_quotes(s)
    s = _escape_controls(s)
    # 去掉尾逗号
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # 收尾时多打一个引号：``...,"}}`` / ``...,"}``（真实日志里出现过，
    # 会让 json 报 "Unterminated string"，整份响应掉进逐字段抢救）
    s = re.sub(r',\s*"\s*([}\]])$', r"\1", s)
    s = re.sub(r',\s*"\s*$', "", s)
    # 补全缺失的右括号
    open_curly = s.count("{") - s.count("}")
    open_sq = s.count("[") - s.count("]")
    if open_sq > 0:
        s += "]" * open_sq
    if open_curly > 0:
        s += "}" * open_curly
    return s


def _loads_dict(value: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _raw_decode_dict(value: str) -> dict[str, Any] | None:
    """从第一个对象开始解析，忽略后面的垃圾（模型常包一层 [[[...]]]）。"""
    start = value.find("{")
    if start < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(value[start:])
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def try_json(text: str) -> tuple[dict[str, Any] | None, str, list[str]]:
    warnings: list[str] = []
    candidates = [text.strip(), strip_fences(text)]
    for cand in candidates:
        if not cand:
            continue
        repaired = _repair_json(cand)
        for label, value in (("strict", cand), ("repair", repaired)):
            obj = _loads_dict(value)
            if obj is not None:
                return obj, label, warnings
        span = _find_json_span(cand)
        if span:
            span_repaired = _repair_json(span)
            for label, value in (("span", span), ("span+repair", span_repaired)):
                obj = _loads_dict(value)
                if obj is not None:
                    return obj, label, warnings
            # 定界扫描可能被字符串里的野引号带偏，raw_decode 只认对象本身
            for label, value in (("raw_decode", span), ("raw_decode+repair", span_repaired)):
                obj = _raw_decode_dict(value)
                if obj is not None:
                    return obj, label, warnings
    return None, "none", warnings


def try_salvage(text: str) -> tuple[dict[str, Any] | None, str, list[str]]:
    """整体解析失败后的最后一道自动修复：逐字段抢救。

    真实样本里最常见的失败是"某处引号失配 / 数字被折行劈开"，
    整体 ``json.loads`` 直接报废，但每个字段本身是完整的。
    这里按字段头切开单独清理，通常能救回 90% 以上内容。
    """
    warnings: list[str] = []
    for cand in (text, strip_fences(text)):
        if not cand:
            continue
        fields = salvage_fields(cand)
        if fields.get("question_text"):
            warnings.append("结构化输出已损坏，已按字段逐个抢救恢复（部分字段可能缺失）")
            return fields, "salvage", warnings
    return None, "none", warnings


def _canonical_key(key: str) -> str | None:
    k = key.strip().lower().replace(" ", "_").replace("-", "_")
    for canon, aliases in FIELD_ALIASES.items():
        if k == canon or k in [a.lower() for a in aliases]:
            return canon
    for canon, aliases in FIELD_ALIASES.items():
        for a in aliases:
            if a and a.lower() in k:
                return canon
    return None


def normalise_dict(obj: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        canon = _canonical_key(str(key))
        if not canon:
            continue
        if isinstance(value, (list, tuple)):
            if canon in ("knowledge_points",):
                out[canon] = [str(v).strip() for v in value if str(v).strip()]
            elif canon in ("figure_smiles", "figure_flow"):
                # 结构式/流程图本身就是结构化的，拼成字符串反而丢结构
                out[canon] = list(value)
            else:
                out[canon] = "\n".join(str(v) for v in value)
        else:
            out[canon] = value
    return out


def parse_sections(text: str) -> dict[str, str]:
    """按 [[标记]] 或中文小标题切分正文。"""
    out: dict[str, str] = {}
    marks = list(SECTION_MARK.finditer(text))
    if marks:
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.end() : end].strip()
            canon = _canonical_key(m.group(1))
            if canon:
                out[canon] = body
        if out:
            return out
    heads = list(CN_HEADING.finditer(text))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        canon = _canonical_key(m.group(1))
        body = text[m.end() : end].strip()
        body = re.sub(r"^[-*]\s*", "", body)
        if canon and body:
            out[canon] = body
    return out


def _coerce_number(value: Any, lo: float, hi: float) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return max(lo, min(hi, value))
    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not m:
        return None
    num = float(m.group())
    if num == int(num) and hi > 10:
        return int(num)
    return max(lo, min(hi, num))


def _is_cjk(ch: str) -> bool:
    return bool(ch) and "\u4e00" <= ch <= "\u9fff"


_LITERAL_ESCAPE = re.compile(r"\\([nrt])")


def unescape_literals(text: str) -> str:
    """把正文里的**字面量** ``\\n`` / ``\\r`` / ``\\t`` 还原成真控制字符。

    模型偶尔把 JSON 写成 ``"stem": "...\\nA. 选项"``，多转义了一层：JSON 解出来
    是两个字符 ``\\`` + ``n``。页面按 Markdown 渲染时它既不是换行、进 KaTeX 还会
    直接报错，于是整段变成一坨带 ``\\n`` 的文字。

    难点是 TeX 里 ``\\n`` / ``\\r`` / ``\\t`` 全是命令前缀：``\\notin`` 的字母段是
    ``notin``，``\\theta`` 是 ``heta``，更短的 ``\\ne`` ``\\to`` ``\\nu`` 只差一个字母。
    所以判据不是"后面跟几个字母"，而是把整个词（含被匹配的首字母）丢进
    :data:`TEX_COMMANDS` 查：``ne`` / ``to`` / ``theta`` / ``notin`` 命中即保留；
    ``\\nA. ` → ``nA`` 不命中 → 还原成换行 + ``A. ``。
    """
    if "\\" not in text:
        return text

    def repl(m: re.Match[str]) -> str:
        i = m.start() + 1
        j = i
        while j < len(text) and text[j].isascii() and text[j].isalpha():
            j += 1
        if _is_tex_command(text[i:j]):
            return m.group(0)
        return {"n": "\n", "r": "\r", "t": "\t"}[m.group(1)]

    return _LITERAL_ESCAPE.sub(repl, text)


def _reflow(text: str) -> str:
    """规范换行。

    模型用 ``\\n`` 显式写的换行是**语义换行**（分段、选项分行），必须保留；
    真正的排版硬折行已经在 :func:`_escape_controls` 里删掉了。
    唯一的例外：两侧都是汉字且前一行没有标点收尾 —— 那一定是折行断在句中，
    合并时不加空格（中文没有词间空格）。
    """
    if "\n" not in text:
        return text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    blank = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            blank = bool(out)
            continue
        if blank:
            out.append("")
            blank = False
        # 上一行孤零零只剩个标点（"作差法" ⏎ "："），并回去
        if out and out[-1] and re.fullmatch(r"[：:；;，,。.、]+", line.strip()):
            out[-1] += line.strip()
            continue
        # 行首是标点，说明上一行的断行把标点甩了下来
        if out and out[-1] and re.match(r"^[：:；;，,、]", line.strip()):
            out[-1] += line.strip()
            continue
        if out and _is_cjk(out[-1][-1:]) and _is_cjk(line.lstrip()[:1]):
            out[-1] += line.lstrip()
            continue
        out.append(line)
    return "\n".join(out).strip()


def _tidy_math_run(s: str) -> str:
    s = _SPACED_CMD.sub(_join_spaced_command, s)          # \ frac → \frac，\ x 保持
    s = _DOUBLE_ESCAPE_CMD.sub(r"\\", s)                  # \\mathrm → \mathrm
    s = re.sub(r"([\^_])[ \t]+", r"\1", s)               # x^ 2 → x^2
    s = re.sub(r"[ \t]+([\^_])", r"\1", s)               # x ^2 → x^2
    s = re.sub(r"([{([])[ \t]+", r"\1", s)               # \frac{ 1} → \frac{1}
    s = re.sub(r"[ \t]+([}\])])", r"\1", s)              # (0, +\infty ) → (...)
    s = re.sub(r"[ \t]+([,;])", r"\1", s)
    s = re.sub(r"(?<=\d)[ \t]+(?=\d)", "", s)            # 2 026 → 2026
    return s


def _clean_math(body: str) -> str:
    """清理公式体内的排版垃圾。

    只做**安全方向**的收紧：空白紧贴 ``^ _ { } ( ) ,`` 时删除、数字中间的空格删除。
    绝不能全局删空格 —— ``\\ln x`` 会变成 ``\\lnx``（未定义命令），比原问题更糟。
    ``\\text{ ... }`` 里的空格有语义（中英混排留白），原样保留。
    """
    s = body.replace("\r", " ").replace("\n", " ")
    parts = re.split(r"(\\text\{[^{}]*\})", s)
    for i, part in enumerate(parts):
        parts[i] = part if part.startswith("\\text{") else _tidy_math_run(part)
    return "".join(parts).strip()


def normalise_latex(text: str) -> tuple[str, list[str]]:
    """统一公式定界符并修正常见不兼容写法。"""
    warnings: list[str] = []
    if not text:
        return "", warnings
    t = html.unescape(str(text))
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    # 部分模型/网关会在反斜杠后插入空格（\ frac / \ in），或在汉字之间插空格（因 式分解）。
    # 只有"后面确实是命令名"才合并，避免把 ``\ x_2`` 的空格吃掉变成未定义命令。
    t = _SPACED_CMD.sub(_join_spaced_command, t)
    t = _DOUBLE_ESCAPE_CMD.sub(r"\\", t)
    t = re.sub(r"([\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])", r"\1", t)
    # 字面 \n / \t（被 JSON 二次转义留下的两个字符）→ 真正的换行。
    # 必须带字母负向断言：\nu \nabla \neq \notin \times \tau \rho 都是合法命令。
    t = re.sub(r"\\r\\n|\\n(?![A-Za-z])", "\n", t)
    t = re.sub(r"\\n(?=[A-Z][.、)])", "\n", t)      # 折行把 \nD. 劈成了 \ + nD.
    t = re.sub(r"\\t(?![A-Za-z])", " ", t)
    # 折行把 "\nD." 吃成 " nD."（反斜杠已丢失）——只认这种紧贴选项字母的形态
    t = re.sub(r"[ \t]+n(?=[A-Z][.、)])", "\n", t)
    t = re.sub(r"(?<=[，。；：,;])\s*n(?=[\u4e00-\u9fff])", "\n", t)  # "，n因此"
    t = re.sub(r"\\\((.+?)\\\)", lambda m: f"${m.group(1)}$", t, flags=re.S)
    t = re.sub(r"\\\[(.+?)\\\]", lambda m: f"$${m.group(1)}$$", t, flags=re.S)
    t = re.sub(r"\\begin\{equation\*?\}(.+?)\\end\{equation\*?\}", lambda m: f"$${m.group(1).strip()}$$", t, flags=re.S)
    t = re.sub(r"\\begin\{align\*?\}(.+?)\\end\{align\*?\}", lambda m: f"$$\\begin{{aligned}}{m.group(1)}\\end{{aligned}}$$", t, flags=re.S)
    t = re.sub(r"\\tag\{[^}]*\}", "", t)
    t = re.sub(r"\\(?:label|ref)\{[^}]*\}", "", t)
    t = t.replace("\\displaystyle\\frac", "\\dfrac")
    # 常见 Unicode 数学符号 → LaTeX（只在公式内替换）
    mapping = {
        "×": "\\times ",
        "÷": "\\div ",
        "≤": "\\le ",
        "≥": "\\ge ",
        "≠": "\\ne ",
        "≈": "\\approx ",
        "±": "\\pm ",
        "∓": "\\mp ",
        "∞": "\\infty ",
        "π": "\\pi ",
        "α": "\\alpha ",
        "β": "\\beta ",
        "γ": "\\gamma ",
        "θ": "\\theta ",
        "Δ": "\\Delta ",
        "√": "\\sqrt ",
        "∑": "\\sum ",
        "∏": "\\prod ",
        "∫": "\\int ",
        "∈": "\\in ",
        "∉": "\\notin ",
        "⊂": "\\subset ",
        "⊆": "\\subseteq ",
        "∪": "\\cup ",
        "∩": "\\cap ",
        "→": "\\to ",
        "⇒": "\\Rightarrow ",
        "⇔": "\\Leftrightarrow ",
        "∵": "\\because ",
        "∴": "\\therefore ",
        "⊥": "\\perp ",
        "∥": "\\parallel ",
        "∠": "\\angle ",
        "△": "\\triangle ",
        "⊙": "\\odot ",
        "°": "^{\\circ}",
    }

    def fix_math(m: re.Match[str]) -> str:
        body = _clean_math(m.group(2))
        # 不要把已有的 \text{} 内容里的符号替换掉
        parts = re.split(r"(\\text\{[^{}]*\})", body)
        for i, part in enumerate(parts):
            if part.startswith("\\text{"):
                continue
            for uni, latex in mapping.items():
                part = part.replace(uni, latex)
            parts[i] = part
        return m.group(1) + "".join(parts) + m.group(1)

    t = re.sub(r"(\${1,2})(.+?)\1", fix_math, t, flags=re.S)
    # 空格贴到中文标点前（模型常在标点前留空格），以及 "1 ." 这种断号
    t = re.sub(r"[ \t]+([，。；：、！？）」』】])", r"\1", t)
    t = re.sub(r"(?<=\d)[ \t]+([.．、](?!\d))", r"\1", t)
    t = _reflow(t)
    # 左括号后立刻换行 = 折行断在 "(1)" 中间；括号在行尾时行首不会是标点，_reflow 管不到
    t = re.sub(r"([(\[（【])\s*\n\s*", r"\1", t)
    # reflow 会用空格连接断行，这里再清一遍中文字间空格
    t = re.sub(r"([\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])", r"\1", t)
    # $ 数量为奇数说明定界符不配对，提示用户
    if len(re.findall(r"(?<!\\)\$", t)) % 2 == 1:
        warnings.append("公式定界符 $ 数量不成对，可能有公式未闭合")
    # 花括号配对（粗略）
    if t.count("{") != t.count("}"):
        warnings.append("LaTeX 花括号不配对，渲染时可能出错")
    return t.strip(), warnings


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "是", "需要", "含图", "有图")
    return False


_FENCE_RE = re.compile(r"^\s*```+[a-zA-Z]*\s*|\s*```+\s*$")
# 坐标名被折行劈开后补出来的空格：``(A 1)`` —— 只在文中确实存在 ``(A1)`` 时才合并
_TIKZ_SPACED_NAME = re.compile(r"\(([A-Za-z]{1,4}) (\d{1,3})\)")


def _tikz_cleanup(text: str) -> str:
    """修两类只在 TikZ 里会致命的折行残留。

    - 字面 ``\\n``：模型把换行写成 JSON 转义又被二次转义，整段图挤成一行，
      而 ``\\n`` 在 TikZ 里不是合法命令，编译直接失败。带字母负向断言，
      免得把 ``\\node`` / ``\\tikz`` 拆了。
    - 坐标名 ``(B 1)``：折行把 ``B1`` 劈开后补的空格，TikZ 认不出这个名字
      （Package pgf Error: No shape named ``B 1' is known）。只在文中确实
      存在无空格写法时才合并，不动真有空格的自定义节点名。
    """
    text = re.sub(r"\\n(?![A-Za-z])", "\n", text)
    text = re.sub(r"\\t(?![A-Za-z])", " ", text)

    known = set(re.findall(r"\(([A-Za-z]{1,4}\d{1,3})\)", text))
    if known:

        def glue(m: re.Match[str]) -> str:
            name = m.group(1) + m.group(2)
            return f"({name})" if name in known else m.group(0)

        text = _TIKZ_SPACED_NAME.sub(glue, text)
    return text


def _normalise_flow(raw: Any) -> dict[str, Any]:
    """把模型给的工艺流程图描述收成标准结构。

    字段别名、字符串链条（``A→B→C``）、dict/list 容器这些形态归一，全部交给
    ``app.render.flow.normalise`` —— 那份是唯一事实来源。以前这里另写了一套，
    结果 flow 加了 ``smiles`` 之后解析阶段就把结构式吃掉了，编辑器里画得出来、
    识别结果里却没有，很难查。

    步骤不足两步（或描述根本不像流程图）时返回空，上层据此退回贴原题截图。
    """
    from ..render import flow as flow_mod

    try:
        return flow_mod.normalise(raw)
    except Exception:  # noqa: BLE001 - 解析阶段宁可丢图，也不能让整题失败
        return {}


def _normalise_smiles(raw: Any) -> dict[str, str]:
    """把模型给的 smiles 收成 ``{名称: SMILES}``。

    这里**只做形态归一**（dict / list / 裸字符串 / 一行一个的文本都见过），
    不做化学合法性校验 —— 那一步交给 ``app.render.chem``，避免解析阶段就把
    RDKit 的依赖和启动开销背进来。
    """
    items: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, (list, tuple)):
                items.extend((str(key), str(one)) for one in value)
            else:
                items.append((str(key), str(value)))
    elif isinstance(raw, (list, tuple)):
        for one in raw:
            if isinstance(one, dict):
                items.extend((str(k), str(v)) for k, v in one.items())
            else:
                items.append(("", str(one)))
    elif isinstance(raw, str) and raw.strip():
        for line in raw.splitlines():
            line = line.strip().strip(",")
            if not line:
                continue
            parts = re.split(r"[:：]", line, maxsplit=1)
            if len(parts) == 2:
                items.append((parts[0], parts[1]))
            else:
                items.append(("", line))

    out: dict[str, str] = {}
    for name, value in items:
        smi = re.sub(r"\s+", "", str(value)).strip("`'\"")
        if not smi:
            continue
        label = str(name).strip().strip("`'\"、 ") or f"结构式{len(out) + 1}"
        if label in out:
            continue
        out[label] = smi
    return out


def clean_tikz(raw: object) -> str:
    """收一下模型给的 TikZ：去 Markdown 围栏、修折行残留、补全环境、限长。

    这里**不做** LaTeX 规范化（合并空格、补反斜杠那套）——TikZ 代码里的
    空格和反斜杠都是有意义的，动一下就编译不出来。

    但环境本身要补：模型输出被 max_tokens 截断时，末尾常是
    ``\\end{tikzpicture``（少个花括号）或者半条 ``\\draw``，存进库里这张图
    就永远渲染不出来。补全逻辑与渲染层共用 :func:`app.render.tikz.repair_environment`，
    保证"存进去的"和"渲染用的"是同一份代码。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = _FENCE_RE.sub("", text).strip()
    text = _tikz_cleanup(text)[:8000]
    try:
        from ..render.tikz import repair_environment

        text = repair_environment(text)[0]
    except Exception:  # noqa: BLE001 - 补不出来就原样存，渲染层还会再试一次
        pass
    return text[:8000]


def _clean_svg(raw: Any) -> tuple[str, bool]:
    """返回 (安全 SVG, 原始内容是否存在但被拒绝)。"""
    if raw is None:
        return "", False
    if isinstance(raw, dict):
        raw = raw.get("svg") or raw.get("code") or json.dumps(raw, ensure_ascii=False)
    text = str(raw).strip()
    if not text or "<svg" not in text.lower():
        return "", bool(text)
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        from ..render.svg import sanitize_svg

        return sanitize_svg(text) or "", True
    except Exception:  # noqa: BLE001 - 过滤失败一律按"需要原图"处理
        return "", True


_LEADING_NO = re.compile(r"^\s*(?:第\s*)?\d{1,3}\s*[.、．)）]\s*(?=\D)")


def strip_question_number(text: str) -> str:
    r"""去掉模型照抄试卷的原题号（卷面会自己重新编号）。

    只在 ``question_text`` 上调用；解答里的 "1. ..." 是有意义的步骤编号，不能动。
    ``(?=\D)`` 保证 "1.5 倍" 这类小数不被误切。
    """
    return _LEADING_NO.sub("", text or "", count=1)


_SCHEMA_KEYS = (
    "question_text",
    "question_type",
    "answer",
    "error_analysis",
    "solution_fix",
    "solution_alt",
    "notes",
    "knowledge_points",
    "difficulty",
    "confidence",
    "needs_figure",
    "figure_tikz",
    "figure_svg",
    "figure_smiles",
    "figure_flow",
)

# 值末尾混进来的下一段 JSON：``…应为 $\frac{1}{\sqrt{3}}$。","knowledge_points":[…]``
_JSON_TAIL = re.compile(
    r"\s*[\"']?\s*,?\s*[\"']?(?:%s)[\"']?\s*:\s*[\[{\"'0-9tfn-]" % "|".join(_SCHEMA_KEYS)
)


def strip_json_tail(value: str) -> tuple[str, bool]:
    """砍掉正文字段末尾混进来的 JSON 字段（``…。","knowledge_points":[…]``）。

    任何一条解析路径漏了边界，最终都会在这里被截住 —— 用户看到的是
    「另解写到一半突然冒出一串 knowledge_points / difficulty / needs_figure」，
    那种内容一旦印到试卷上就没法看了。
    只认**我们自己的字段名 + 冒号**，正文里正常出现这些词（比如「答案：」）
    不会被误伤。
    """
    if not value:
        return value, False
    cut = False
    m = _JSON_TAIL.search(value)
    if m and m.start() > 0:
        value = value[: m.start()]
        cut = True
    # 收尾残留的孤双引号 / 半个括号：``…$。","`` / ``…$。"}``
    # 只认双引号 —— 单引号在数学里是撇号（$B'$），不能当垃圾删。
    tail = re.search(r'"[^"]*$', value)
    if tail and not value[tail.start() + 1 :].strip(" \t\r\n,}{[]:\"'"):
        value = value[: tail.start()]
        cut = True
    if cut:
        value = re.sub(r"[\s,]+$", "", value).rstrip("}][{\"").rstrip()
    return value, cut


def finalise(obj: dict[str, Any], strategy: str, warnings: list[str] | None = None) -> ParsedResult:
    data = normalise_dict(obj)
    warns = list(warnings or [])
    for key in ("question_text", "answer", "error_analysis", "solution_fix", "solution_alt", "notes"):
        if key in data and not isinstance(data[key], str):
            data[key] = json.dumps(data[key], ensure_ascii=False) if isinstance(data[key], (dict, list)) else str(data[key])
        probe = data.get(key, "") or ""
        if isinstance(probe, str):
            probe, cut = strip_json_tail(probe)
            if cut:
                warns.append(f"{key} 末尾混入了 JSON 残留（knowledge_points / difficulty 等），已截断")
        probe = unescape_literals(probe)
        value, w = normalise_latex(probe)
        if key == "question_text":
            value = strip_question_number(value)
        data[key] = value
        warns.extend(w)
    kp = data.get("knowledge_points")
    if isinstance(kp, str):
        data["knowledge_points"] = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"[、,，;；/|]", kp) if p.strip()]
    elif isinstance(kp, list):
        data["knowledge_points"] = [re.sub(r"\s+", " ", str(p)).strip() for p in kp if str(p).strip()]
    else:
        data["knowledge_points"] = []
    if "question_type" in data:
        data["question_type"] = str(data["question_type"]).strip()[:20] or None
    data["difficulty"] = _coerce_number(data.get("difficulty"), 1, 5)
    data["confidence"] = _coerce_number(data.get("confidence"), 0, 1)
    tikz = clean_tikz(data.get("figure_tikz"))
    # 模型偶尔会把 SVG 塞进 TikZ 字段（历史习惯），转回旧通道别丢图
    if tikz and "<svg" in tikz.lower():
        data.setdefault("figure_svg", tikz)
        tikz = ""
    data["figure_tikz"] = tikz

    svg, svg_rejected = _clean_svg(data.get("figure_svg"))
    data["figure_svg"] = svg
    # 化学结构式存成 JSON 文本（DB 字段是 TEXT），代码里统一按 dict 用
    smiles = _normalise_smiles(data.get("figure_smiles"))
    data["figure_smiles"] = json.dumps(smiles, ensure_ascii=False) if smiles else ""
    flow = _normalise_flow(data.get("figure_flow"))
    data["figure_flow"] = json.dumps(flow, ensure_ascii=False) if flow else ""
    needs = _coerce_bool(data.get("needs_figure")) or svg_rejected
    if tikz or smiles or flow:
        # 有 TikZ / 结构式 / 流程图就不需要再贴原题截图；真渲染失败时由 client 层翻回 true。
        # 手绘 SVG（文本模式的历史通道）不在此列：模型既给了 SVG 又标了 needs_figure=true 时，
        # 以它自己的判断为准 —— 说明它认为这图（多半是装置图）贴原图更靠谱
        needs = False
    data["needs_figure"] = bool(needs)
    if svg_rejected:
        warns.append("AI 给出的图形 SVG 无法安全解析，已改为保留原题截图")
    if not data.get("question_text"):
        warns.append("未能解析出题目正文")
        return ParsedResult(data=data, strategy=strategy, degraded=True, warnings=warns)
    # 文本模式与"逐字段抢救"都需要人工过一遍
    return ParsedResult(
        data=data,
        strategy=strategy,
        degraded=strategy.startswith("text") or "salvage" in strategy,
        warnings=warns,
    )


def parse(raw: str, *, strict: bool = True) -> ParsedResult:
    """解析模型输出。

    ``strict=True`` 时，如果响应**明显是 JSON** 却怎么都解析不出来，
    会抛 :class:`ParseError` 而不是把原始 JSON 当题干塞进题库 ——
    那种"静默降级"会让用户看到满屏 ``\\frac``，比直接报错更难处理。
    ``strict=False`` 保留旧的兜底行为，供修复工具/测试使用。
    """
    text, fixed_mojibake = repair_mojibake((raw or "").strip())
    if not text:
        return ParsedResult(data={}, strategy="empty", degraded=True, warnings=["模型返回空内容"])
    pre_warns = ["响应疑似 UTF-8 被错误解码，已自动还原"] if fixed_mojibake else []

    obj, how, warns = try_json(text)
    if obj is not None:
        res = finalise(obj, f"json:{how}", pre_warns + warns)
        if res.data.get("question_text"):
            return res
        warns.extend(res.warnings)
        # JSON 结构通了但没题干，同样不能当结果用 —— 建出来的会是一道空题。
        if strict:
            raise ParseError(
                "AI 返回的 JSON 缺少题目正文",
                raw=text,
                detail="字段 question_text 为空或未识别",
            )
        return res
    else:
        obj, how, salv_warns = try_salvage(text)
        if obj is not None:
            res = finalise(obj, f"json:{how}", pre_warns + warns + salv_warns)
            if res.data.get("question_text"):
                return res
            warns.extend(salv_warns)

    sections = parse_sections(text)
    if sections:
        return finalise(sections, "text:sections", pre_warns + warns)

    if strict and looks_like_json(text):
        detail = _json_failure_detail(text)
        raise ParseError(
            "AI 返回的是 JSON，但结构损坏到无法解析",
            raw=text,
            detail=detail,
        )

    # 最后兜底：整段当题干
    return finalise(
        {"question_text": text, "notes": "模型未按格式输出，已把原始内容放入题干"},
        "text:fallback",
        pre_warns + warns,
    )


def _json_failure_detail(text: str) -> str:
    """给出人能看懂的失败位置，便于排查是模型还是网关的问题。"""
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        start = max(0, exc.lineno - 1)
        lines = text.split("\n")
        line = lines[start] if start < len(lines) else ""
        return f"{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）：{line.strip()[:160]}"
    except ValueError as exc:  # 极少见
        return str(exc)[:200]
    return "结构不完整或长度被截断"
