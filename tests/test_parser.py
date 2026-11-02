"""AI 输出解析的鲁棒性测试。"""
from __future__ import annotations

import json

import pytest

from app.ai import parser

GOOD = {
    "question_text": "已知函数 $f(x)=x^2-4x+3$，求最小值。",
    "question_type": "解答题",
    "answer": "$-1$",
    "error_analysis": "没有验证对称轴位置。",
    "solution_fix": "$$f(x)=(x-2)^2-1$$",
    "solution_alt": "",
    "knowledge_points": ["二次函数", "最值"],
    "difficulty": 2,
    "confidence": 0.9,
    "notes": "",
}


def test_strict_json():
    res = parser.parse(json.dumps(GOOD, ensure_ascii=False))
    assert res.strategy == "json:strict"
    assert res.data["question_text"] == GOOD["question_text"]
    assert res.data["knowledge_points"] == ["二次函数", "最值"]
    assert res.data["difficulty"] == 2


def test_json_in_code_fence():
    raw = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    res = parser.parse(raw)
    assert res.strategy.startswith("json")
    assert not res.warnings or "定界符" not in "".join(res.warnings)


def test_json_with_leading_prose():
    raw = "好的，下面是整理结果：\n" + json.dumps(GOOD, ensure_ascii=False) + "\n希望有帮助。"
    res = parser.parse(raw)
    assert res.strategy.startswith("json")
    assert "x^2-4x+3" in res.data["question_text"]


def test_raw_newlines_inside_strings():
    """模型把 \\n 写成真实换行（最常见的破坏形式）。"""
    raw = (
        '{\n  "question_text": "已知函数 $f(x)=x^2-4x+3$，\n求它在区间 $[0,3]$ 上的最小值。",\n'
        '  "answer": "$-1$",\n  "error_analysis": "没有验证\n对称轴位置。",\n'
        '  "knowledge_points": ["二次函数"],\n  "difficulty": 2\n}'
    )
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    assert "求它在区间" in res.data["question_text"]
    assert res.data["error_analysis"].startswith("没有验证")


def test_single_backslash_latex():
    """LaTeX 命令只写了一个反斜杠，属于非法 JSON 转义。"""
    raw = '{"question_text": "求 $\\frac{1}{2}+\\sqrt{3}$ 的值", "answer": "1"}'
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    assert "\\frac{1}{2}" in res.data["question_text"]


def test_backslash_newline_break():
    raw = '{"question_text": "$$\\\n\\Delta = b^2-4ac$$", "answer": "$\\Delta$"}'
    res = parser.parse(raw)
    assert res.strategy.startswith("json")
    assert "\\Delta" in res.data["question_text"]


def test_trailing_comma_and_missing_brace():
    raw = '{"question_text": "题目", "answer": "1", "knowledge_points": ["a", "b",],}'
    res = parser.parse(raw)
    assert res.strategy.startswith("json")
    assert res.data["knowledge_points"] == ["a", "b"]


def test_section_markers_output():
    raw = """[[题目]]
已知 $a>0$，求 $a+\\frac{1}{a}$ 的最小值。

[[题型]]
解答题

[[答案]]
$2$

[[错误原因]]
没有说明取等条件。

[[纠正解析]]
由基本不等式 $a+\\frac{1}{a}\\ge 2$，当且仅当 $a=1$ 取等号。

[[另解]]

[[知识点]]
基本不等式、最值

[[难度]]
2

[[置信度]]
0.95
"""
    res = parser.parse(raw)
    assert res.strategy == "text:sections"
    assert res.data["answer"] == "$2$"
    assert res.data["knowledge_points"] == ["基本不等式", "最值"]
    assert res.data["difficulty"] == 2
    assert "纠正解析" not in res.data["solution_fix"]


def test_chinese_headings_output():
    raw = """题目：解方程 $2x^2-5x+2=0$。
答案：$x_1=\\frac{1}{2}$，$x_2=2$。
错误原因：因式分解时符号写错。
解析：$(2x-1)(x-2)=0$，故 $x=\\frac{1}{2}$ 或 $x=2$。
"""
    res = parser.parse(raw)
    assert res.strategy == "text:sections"
    assert "2x^2-5x+2=0" in res.data["question_text"]
    assert "2x-1" in res.data["solution_fix"]


def test_plain_text_fallback():
    raw = "这道题考查二次函数的最值，学生没有配方。"
    res = parser.parse(raw)
    assert res.strategy == "text:fallback"
    assert res.degraded
    assert res.data["question_text"] == raw


def test_empty_output():
    res = parser.parse("   ")
    assert res.strategy == "empty"
    assert res.degraded


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("\\[x^2\\]", "$$x^2$$"),
        ("\\(a+b\\)", "$a+b$"),
        ("$x^{2}$，$\\alpha$", "$x^{2}$，$\\alpha$"),
    ],
)
def test_latex_delimiters(raw, expect):
    out, _warnings = parser.normalise_latex(raw)
    assert out == expect


def test_unicode_math_to_latex():
    out, _ = parser.normalise_latex("$3×4≤12$，$π≈3.14$")
    assert "\\times" in out and "\\le" in out and "\\pi" in out
    # 中文字符间的空格被清理
    out2, _ = parser.normalise_latex("因 式 分 解")
    assert out2 == "因式分解"


def test_backslash_space_repair():
    out, _ = parser.normalise_latex("$\\ frac{1}{2}$")
    assert "\\frac{1}{2}" in out.replace(" ", "")


def test_reflow_joins_wrapped_lines():
    out, _ = parser.normalise_latex("第一行内\n容接着写\n\n第二段")
    assert "第一行内容接着写" in out
    assert out.split("\n")[-1] == "第二段"


def test_knowledge_points_from_string():
    res = parser.finalise({"question_text": "题目", "knowledge_points": "二次函数、最值, 配方法"}, "json:strict")
    assert res.data["knowledge_points"] == ["二次函数", "最值", "配方法"]


def test_alias_keys():
    res = parser.parse('{"题目": "题目内容", "答案": "1", "错因": "粗心"}')
    assert res.data["question_text"] == "题目内容"
    assert res.data["answer"] == "1"
    assert res.data["error_analysis"] == "粗心"


def test_missing_question_text_is_degraded():
    """JSON 通了但没有题干 = 无法建题，必须报错而不是造一道空题。

    宽松模式下（修复工具用）仍返回 degraded，便于人工判断。
    """
    with pytest.raises(parser.ParseError) as box:
        parser.parse('{"answer": "1"}')
    assert "题目正文" in str(box.value)

    res = parser.parse('{"answer": "1"}', strict=False)
    assert res.degraded
    assert any("题目正文" in w for w in res.warnings)


# ---------------------------------------------------------------------------
# 模型输出按固定宽度硬折行（真实踩坑，2026-10-29）
# 换行点会落在 \frac{ 与 1} 之间、\\ 与 mathrm 之间、甚至 JSON 的 key 中间。
# 这些换行不携带语义，必须整段删除；早期实现转成空格/保留，导致渲染全崩。
# ---------------------------------------------------------------------------

def test_hardwrap_inside_frac_keeps_command():
    raw = '{"question_text": "已知 $\\frac{\n1}{2}$ 的值。", "answer": "$\\frac{1}{2}$"}'
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    assert "\\frac{1}{2}" in res.data["question_text"]
    assert "\\frac{\n" not in res.data["question_text"]


def test_hardwrap_between_backslash_and_command():
    # JSON 里是 \\（转义后的单反斜杠）被换行劈成 "\" + 换行 + "mathrm"
    raw = '{"question_text": "已知 $\\\\\nmathrm{e}^{x}$ 单调递增。"}'
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    assert "\\mathrm{e}^{x}" in res.data["question_text"]


def test_hardwrap_splits_escaped_backslash_pair():
    r"""换行落在 ``\\`` 的两个反斜杠中间（真实踩坑，2026-10-30）。

    模型写 ``$\mathrm{e}^x``（JSON 里是 ``\\mathrm``），折行把它劈成
    ``"\$⏎\mathrm``。两半各补一个反斜杠就变成 ``\\mathrm`` —— LaTeX 里
    ``\\`` 是换行命令，``mathrm`` 退化成一串斜体字母，页面上凭空多出
    "mathrm"。必须只留一个反斜杠。
    """
    raw = '{"question_text": "已知不等式 $\\\n\\mathrm{e}^x - m - \\ln(x+m) \\ge 0$ 恒成立。"}'
    res = parser.parse(raw)
    got = res.data["question_text"]
    assert res.strategy.startswith("json"), res.strategy
    assert "$\\mathrm{e}^x" in got
    assert "\\\\mathrm" not in got


def test_double_escape_command_collapsed():
    """已入库的旧数据：``\\mathrm``（两个反斜杠）收敛回一个。"""
    raw = '{"question_text": "$\\\\mathrm{e}^x$", "answer": "$\\\\text{恒成立}$"}'
    res = parser.parse(raw)
    assert res.data["question_text"] == "$\\mathrm{e}^x$"
    assert res.data["answer"] == "$\\text{恒成立}$"


def test_double_escape_keeps_real_line_break():
    """``\\`` 后跟正常文字（对齐环境的换行）不能被当成双重转义。"""
    raw = '{"question_text": "$$\\\\begin{aligned}a&=1\\\\\\\\b&=2\\\\end{aligned}$$"}'
    res = parser.parse(raw)
    assert "a&=1\\\\b&=2" in res.data["question_text"]


def test_hardwrap_after_underscore_keeps_subscript():
    raw = '{"question_text": "已知 $\\log_\na 2026 > 0$。"}'
    res = parser.parse(raw)
    assert "\\log_a" in res.data["question_text"]
    assert "\\log_ a" not in res.data["question_text"]


def test_hardwrap_letter_digit_stays_glued():
    r"""``B1`` / ``A1`` 这类标识符被折行劈开时不能补空格（真实踩坑，2026-10-30）。

    折行把 TikZ 的 ``(B1)`` 劈成 ``(B``⏎``1)``，早期补空格规则把数字也当
    "单词字符"，于是补成 ``(B 1)`` —— TikZ 认不出这个坐标名，编译直接失败
    （Package pgf Error: No shape named 'B 1' is known）。
    """
    raw = (
        '{"question_text": "如图。", "figure_tikz": "\\\\begin{tikzpicture}\\n'
        '\\\\coordinate (A1) at (0,0);\\n'
        '\\\\coordinate (B\n1) at (1,1);\\n'
        '\\\\draw (A1) -- (B1);\\n'
        '\\\\end{tikzpicture}"}'
    )
    tikz = parser.parse(raw).data["figure_tikz"]
    assert "(B1)" in tikz
    assert "(B 1)" not in tikz


def test_hardwrap_between_english_words_keeps_space():
    """英文单词中间断行仍然要补空格，别把 "numer ator" 粘成一坨。"""
    raw = '{"question_text": "the numer\nator of 分式"}'
    assert parser.parse(raw).data["question_text"] == "the numer ator of 分式"


def test_hardwrap_digits_glued_not_spaced():
    """数字被折行劈开：粘回一个数，而不是中间多一个空格。"""
    raw = '{"question_text": "共 20\n26 名学生"}'
    assert parser.parse(raw).data["question_text"] == "共 2026 名学生"


def test_hardwrap_inside_json_key():
    """换行把 "solution_alt" 劈成 "solution_\\nalt"，不能因此丢掉整个解析。"""
    raw = '{"question_text": "题目", "answer": "1", "solution_\nalt": "另解内容"}'
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), f"降级了：{res.strategy}"
    assert res.data["solution_alt"] == "另解内容"


def test_literal_backslash_n_becomes_newline():
    """被二次转义的字面 \\n（两个字符）要还原成换行。"""
    raw = '{"question_text": "第一句。\\\\n第二句。"}'
    res = parser.parse(raw)
    assert "\n" in res.data["question_text"]
    assert "\\n" not in res.data["question_text"]


def test_greek_nu_not_turned_into_newline():
    """\\nu 是以 \\n 开头的合法命令，绝不能被当成换行。"""
    out, _ = parser.normalise_latex("$\\nu + \\nabla f$")
    assert "\\nu" in out and "\\nabla" in out


def test_text_block_keeps_spaces():
    out, _ = parser.normalise_latex("$\\min\\{a,b\\}=0 \\text{ 且 } b\\ge 0$")
    assert "\\text{ 且 }" in out


def test_no_space_injected_into_command_argument():
    """\\ln x 里的空格有语义：删掉会变成未定义命令 \\lnx。"""
    out, _ = parser.normalise_latex("$y = \\ln x$")
    assert "\\lnx" not in out
    assert "\\ln x" in out


def test_wrapped_response_with_trailing_brackets():
    """模型把 JSON 包在 [[[...]]] 里，且字符串内有野引号导致定界扫描失败。"""
    raw = (
        '[[[\n{\n  "question_text": "已知 $\\log_\na 2026 > 0$",\n'
        '  "answer": "AC",\n  "notes": "说明"\n}\n]]]'
    )
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    assert "AC" == res.data["answer"]


# ---------------------------------------------------------------------------
# 2026-10-29 第二轮真实故障（用户贴的导数压轴题，每次必失败）
# 核心：模型输出里的中文引号被旧代码转成 ASCII 双引号 → 字符串提前闭合。
# ---------------------------------------------------------------------------

def test_chinese_quotes_inside_value_do_not_break_json():
    """「三线合一」这类引用内容不能被当成 JSON 定界符。"""
    raw = (
        '{"question_text": "如图，作 $AD\\\\perp BC$。",\n'
        '  "solution_fix": "由等腰三角形“三线合一”性质得 $BD=3$，\n'
        '所以面积为 $12$。"}'
    )
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    assert "三线合一" in res.data["solution_fix"]
    assert "BD=3" in res.data["solution_fix"]


def test_number_split_by_hardwrap():
    """'"confidence": 1⏎.0' —— 数字被折行劈成两半。"""
    raw = '{"question_text": "题目", "difficulty": 4, "confidence": 1\n.0}'
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    assert res.data["confidence"] == 1.0


def test_backslash_space_before_command_name():
    r"""网关会把 ``\in`` 写成 ``\ in``（反斜杠 + 空格）。"""
    raw = '{"question_text": "已知 $x \\ in (0, 1)$，求 $k \\ ge \\ frac{1}{2}$。"}'
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    stem = res.data["question_text"]
    assert r"\in" in stem and r"\ ge" not in stem
    assert r"\ge" in stem and r"\frac" in stem


def test_backslash_space_keeps_control_space_before_variable():
    r"""``\ x_2`` 是 LaTeX 控制空格 + 变量，不能合并成未定义命令 ``\x_2``。"""
    raw = '{"question_text": "$x_1 = \\\\frac{1}{2},\\\\ x_2 = 2$"}'
    res = parser.parse(raw)
    assert r"\ x_2" in res.data["question_text"]
    assert r"\x_2" not in res.data["question_text"]


def test_answer_newline_after_open_paren():
    """答案写成 "(⏎1) ..."：括号被折行断开，要接回去。"""
    raw = '{"question_text": "题目", "answer": "(\\n1) $k\\\\ge 1$；(\\n2) 见解析。"}'
    res = parser.parse(raw)
    assert res.data["answer"].startswith("(1)")


def test_mojibake_is_repaired():
    """UTF-8 被当 cp1252 解码：'求导得' → 'æ±‚å¯¼å¾—'。"""
    good = "求导得：切线放缩"
    broken = good.encode("utf-8").decode("cp1252")
    raw = '{"question_text": "题目", "solution_fix": "%s"}' % broken
    res = parser.parse(raw)
    assert "求导得" in res.data["solution_fix"]
    assert any("UTF-8" in w for w in res.warnings)


def test_unrecoverable_json_raises_instead_of_dumping_raw():
    """彻底解析不出来时必须报错，绝不能把原始 JSON 当题干塞进题库。"""
    raw = '{"question_text" "已知 $f(x)=k\\ln x$", "answer": "1"}'
    with pytest.raises(parser.ParseError) as box:
        parser.parse(raw)
    assert "JSON" in str(box.value)
    assert box.value.raw  # 带原始响应供排查
    # 宽松模式保留旧行为，方便修复工具判断
    res = parser.parse(raw, strict=False)
    assert res.strategy == "text:fallback"


def test_salvage_recovers_fields_from_wrecked_json():
    """整体结构报废但字段完整时，逐字段抢救。"""
    raw = (
        '{"question_text": "已知函数 $f(x)=k \\\\ln x$，求 $k$。",\n'
        ' "answer": "(1) $k \\\\ge \\\\frac{1}{e}$",\n'
        ' "error_analysis": "学生写成 $(1, +\\\\infty)$。"\n'
        ' "solution_fix": "求导得 $f\'(x)=\\\\frac{k}{x}-e^{-x}$。"\n'
        '}'
    )
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    assert "已知函数" in res.data["question_text"]
    assert "求导得" in res.data["solution_fix"]


def test_looks_like_json_detection():
    assert parser.looks_like_json('{"question_text": "x"}')
    assert parser.looks_like_json('```json\n{"answer": 1}\n```')
    assert parser.looks_like_json('[[[\n{"questio\nn_text": "x"}\n]]]')
    assert not parser.looks_like_json("[[题目]]\n已知函数…")
    assert not parser.looks_like_json("这道题考查导数的单调性。")


def test_gateway_mangled_derivative_question_parses():
    r"""真实样本：网关把 ``\in`` 写成 ``\ in``、把括号后的换行写进字符串。

    这道导数压轴题此前"每次都失败"——JSON 里到处是 ``\ `` 非法转义，
    解析失败后整段原始 JSON 被当成题干。
    """
    raw = (
        '{\n  "question_text": "已知函数 $f(x)= k \\ in \\mathbf{R}$. \\n\\n'
        '(1) 若函数 $y=f(x)$ 为增函数，求 $k$ 的取值范围；\\n\\n'
        '(2) 已知 $0<x_1<x_2 $.",\n'
        '  "question_type": "解答题",\n'
        '  "answer": "(\\n1) $k$ 的取值范围为 $\\left[\\frac{1}{\\mathrm{e}}, +\n'
        '\\infty\\right)$；(2) 证明见解析。",\n'
        '  "error_analysis": "卡点在第 (2) 问：未能把 $x_2$ 视为自变量构造 $h(x)$。",\n'
        '  "solution_fix": "解：\\n(1) 定义域 $(0, +\\infty)$，$f\'(x) = \\ frac{k}{x} - \\ mathrm{e}^{-x}$。\\ n'
        '当 $x \\ in (0, 1)$ 时 $f\'(x)>0$；\\n所以 $k \\ ge \\ frac{1}{\\mathrm{e}}$。",\n'
        '  "difficulty": 4,\n  "confidence": 0.95,\n  "needs_figure": false,\n'
        '  "figure_svg": "",\n  "notes": ""\n}'
    )
    res = parser.parse(raw)
    assert res.strategy.startswith("json"), res.strategy
    stem = res.data["question_text"]
    assert "已知函数" in stem
    assert r"\ in" not in stem and r"\in" in stem
    assert res.data["answer"].startswith("(1)")
    # 公式必须干净：不能残留 "\ frac"、"\ mathrm" 这类带空格的命令
    body = res.data["solution_fix"]
    for bad in (r"\ frac", r"\ mathrm", r"\ ge ", r"\ in "):
        assert bad not in body, f"残留 {bad!r}: {body[:200]}"
    assert r"\frac" in body and r"\mathrm" in body


# ---------------------------------------------------------------------------
# 化学结构式（2026-10-31）：SMILES 走独立字段，四种形态都要收得下来
# ---------------------------------------------------------------------------

def test_smiles_dict_form():
    raw = '{"question_text": "题目", "smiles": {"苯酚": "Oc1ccccc1"}}'
    got = json.loads(parser.parse(raw).data["figure_smiles"])
    assert got == {"苯酚": "Oc1ccccc1"}


def test_smiles_list_and_bare_string():
    raw = '{"question_text": "题目", "smiles": ["CCO"]}'
    assert list(json.loads(parser.parse(raw).data["figure_smiles"]).values()) == ["CCO"]
    raw2 = '{"question_text": "题目", "figure_smiles": "Oc1ccccc1"}'
    assert list(json.loads(parser.parse(raw2).data["figure_smiles"]).values()) == ["Oc1ccccc1"]


def test_smiles_line_per_molecule():
    raw = '{"question_text": "题目", "figure_smiles": "苯酚: Oc1ccccc1\\n甲苯: Cc1ccccc1"}'
    got = json.loads(parser.parse(raw).data["figure_smiles"])
    assert got == {"苯酚": "Oc1ccccc1", "甲苯": "Cc1ccccc1"}


def test_smiles_clears_needs_figure():
    raw = '{"question_text": "题目", "smiles": {"苯酚": "Oc1ccccc1"}, "needs_figure": true}'
    assert parser.parse(raw).data["needs_figure"] is False


# ---------------------------------------------------------------------------
# 折行把反斜杠/命令字劈开（2026-10-31 实拍："text平面"、"sqrt2"）
# 根因：跳过换行时只跳了空格，第二个反斜杠被当成非法转义又补了一遍，
# 于是 \\text 解成 ``\\`` + "text"，页面上凭空多出一串 "text"。
# ---------------------------------------------------------------------------

def test_hardwrap_between_two_backslashes():
    raw = '{"question_text": "$\\\n\\\\text{平面 } AMC$"}'
    res = parser.parse(raw)
    text = res.data["question_text"]
    assert text == r"$\text{平面 } AMC$", repr(text)


def test_hardwrap_inside_command_word():
    # \t 是 JSON 合法转义，折行落在 \t 与 ext 之间时容易解成 TAB + "ext"
    raw = '{"question_text": "$\\t\next{平面 }$"}'
    assert parser.parse(raw).data["question_text"] == r"$\text{平面 }$"
    raw2 = '{"question_text": "$\\f\nrac{\\sqrt2}{2}$"}'
    assert parser.parse(raw2).data["question_text"] == r"$\frac{\sqrt2}{2}$"


def test_hardwrap_after_single_backslash_command():
    raw = '{"question_text": "$\\\nfrac{1}{2}$"}'
    assert parser.parse(raw).data["question_text"] == r"$\frac{1}{2}$"


def test_double_escaped_command_is_collapsed():
    # 历史脏数据兜底：\\sqrt / \\vec / \\angle 也要收敛回单反斜杠
    for cmd in ("sqrt", "vec", "angle", "text", "perp", "parallel"):
        raw = '{"question_text": "$a \\\\%s b$"}' % cmd
        assert parser.parse(raw).data["question_text"] == "$a \\%s b$" % cmd


def test_real_linebreak_still_kept():
    raw = '{"question_text": "$$a=1\\\\\\\\b=2$$"}'
    assert parser.parse(raw).data["question_text"] == "$$a=1\\\\b=2$$"


# ---------------------------------------------------------------------------
# 工艺流程图字段（2026-10-31）：化学流程图交给结构化描述，不让模型算坐标
# ---------------------------------------------------------------------------

FLOW_SPEC = {
    "steps": [{"name": "铝土矿", "note": "Al2O3"}, {"name": "碱浸", "label": "过量 NaOH"}],
    "side": [{"after": 2, "name": "滤渣"}],
}


def test_flow_field_accepts_dict_and_json_text():
    raw = '{"question_text": "流程如图", "flow": %s}' % json.dumps(FLOW_SPEC, ensure_ascii=False)
    res = parser.parse(raw)
    got = json.loads(res.data["figure_flow"])
    assert [s["name"] for s in got["steps"]] == ["铝土矿", "碱浸"]
    assert got["side"][0]["name"] == "滤渣"
    # 有流程图就不该再要求贴原题截图
    assert res.data["needs_figure"] is False


def test_flow_field_tolerates_bare_list_of_steps():
    raw = '{"question_text": "流程如图", "flow": ["浸出", "过滤", "蒸发"]}'
    got = json.loads(parser.parse(raw).data["figure_flow"])
    assert len(got["steps"]) == 3


def test_flow_field_rejects_single_step():
    raw = '{"question_text": "流程如图", "figure_flow": {"steps": [{"name": "只有一步"}]}}'
    assert parser.parse(raw).data["figure_flow"] == ""


def test_flow_field_rejects_plain_prose():
    # 一段描述性文字不是流程图，老老实实退回贴原题截图
    raw = '{"question_text": "流程如图", "figure_flow": "先加碱浸出，再过滤，最后灼烧"}'
    assert parser.parse(raw).data["figure_flow"] == ""


def test_flow_field_accepts_arrow_chain():
    # 模型偶尔直接甩一行「A→B→C」，认出比报错好
    raw = '{"question_text": "流程如图", "figure_flow": "铝土矿->碱浸->过滤"}'
    flow = json.loads(parser.parse(raw).data["figure_flow"])
    assert [s["name"] for s in flow["steps"]] == ["铝土矿", "碱浸", "过滤"]


def test_flow_field_keeps_smiles():
    # 解析阶段曾经把 smiles 吃掉，编辑器里画得出来、识别结果里却没有
    raw = (
        '{"question_text": "合成路线如图", "figure_flow": '
        '{"steps":[{"name":"苯","smiles":"c1ccccc1"},{"name":"苯酚","smiles":"Oc1ccccc1"}]}}'
    )
    flow = json.loads(parser.parse(raw).data["figure_flow"])
    assert [s["smiles"] for s in flow["steps"]] == ["c1ccccc1", "Oc1ccccc1"]


# ---- 字段串味：数组/数字/布尔字段被前一个字段吞掉（真实日志复现）----

LEAKY = (
    '{"question_text":"求二面角的正弦值。","question_type":"解答题",'
    '"answer":"$\\\\frac{\\\\sqrt{6}}{3}$。",'
    '"solution_alt":"设 $\\\\mathbf{u}=\\\\overrightarrow{AP}$，可得余弦为 '
    '$\\\\frac{\\\\sqrt{6}}{3}$。注意题目要求正弦值，故应为 '
    '$\\\\frac{1}{\\\\sqrt{3}}$。",'
    '"knowledge_points":["线面垂直的判定与性质","二面角的平面角"],'
    '"difficulty":4,"confidence":0.62,"needs_figure":true,"}'
)


def test_arrays_and_numbers_do_not_leak_into_solution():
    """``,"knowledge_points":[…]`` 这类尾巴不许跑进另解 —— 会原样印到卷子上。"""
    res = parser.parse(LEAKY, strict=False)
    alt = res.data.get("solution_alt") or ""
    assert "knowledge_points" not in alt
    assert "difficulty" not in alt
    assert 'needs_figure' not in alt
    assert alt.endswith("$\\frac{1}{\\sqrt{3}}$。")
    assert res.data["knowledge_points"] == ["线面垂直的判定与性质", "二面角的平面角"]
    assert res.data["difficulty"] == 4


def test_salvage_keeps_array_fields():
    """整体 JSON 坏掉走逐字段抢救时，数组字段也必须能捞回来。

    早期 ``_FIELD_HEAD`` 只认 ``"key": "``，数组/数字/布尔值的头不算边界，
    于是前一个字段一路吞到文末，知识点、难度全丢，而且污染另解。
    """
    broken = LEAKY.replace('"question_type":"解答题",', '"question_type":"解答题"')  # 故意弄坏结构
    fields = parser.salvage_fields(broken)
    assert fields.get("knowledge_points") == ["线面垂直的判定与性质", "二面角的平面角"]
    assert fields.get("difficulty") == "4"
    assert "knowledge_points" not in (fields.get("solution_alt") or "")


def test_dangling_quote_before_closing_brace_is_repaired():
    """收尾多打一个引号（``…。","}``）不该整份响应降级成逐一抢救。"""
    raw = '{"question_text":"求 $x$。","answer":"$x=1$","notes":"无。","}'
    res = parser.parse(raw, strict=False)
    assert res.strategy.startswith("json:")
    assert res.strategy != "json:salvage"
    assert res.data["answer"] == "$x=1$"


def test_strip_json_tail_keeps_normal_prose():
    """正文里正常出现「答案：」这类词不能被误砍。"""
    text = "另解：由题可知答案：$\\frac{1}{2}$。知识点：二面角。"
    assert parser.strip_json_tail(text)[0] == text


def test_text_mode_figure_tikz_section():
    txt = ("[[题目]]\n如图。\n[[答案]]\n1\n"
           "[[图形TikZ]]\n\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}\n"
           "[[需要原图]]\nfalse\n")
    data = parser.parse(txt, strict=False).data
    assert "tikzpicture" in (data.get("figure_tikz") or "")
    assert data["needs_figure"] is False


# ---------- 字面量 \n 还原 ----------

def test_unescape_literal_newline_before_option():
    """多转义一层留下的两个字面量 ``\\n`` 要变成真换行。

    注意这里是 r-string：JSON 里写 ``\\\\n``（转义的反斜杠 + n），解出来才是
    两个字符的反斜杠 n；写成 ``\\n`` 就成了真换行，测不到这个 bug。
    """
    raw = r'{"question_text":"下列正确的是\\nA. 甲\\nB. 乙","answer":"A"}'
    res = parser.parse(raw, strict=False)
    stem = res.data["question_text"]
    assert "\\n" not in stem
    assert stem.count("\n") >= 2
    assert "A. 甲" in stem


def test_unescape_keeps_tex_commands():
    """``\\notin`` / ``\\ne`` / ``\\theta`` / ``\\to`` / ``\\node`` 一个都不能动。"""
    text = "由 $A\\notin\\alpha$ 且 $x\\ne 0$、$\\theta=\\angle ABC$、$n\\to\\infty$ 得"
    assert parser.unescape_literals(text) == text
    assert parser.unescape_literals("\\node at (0,0) {$A$};") == "\\node at (0,0) {$A$};"


def test_unescape_mixed_positions():
    """只有真字面量被替换，公式部分原样保留。"""
    text = "先判断：\\nA：$a\\ne b$\\nB：$x\\to 0$"
    out = parser.unescape_literals(text)
    assert out == "先判断：\nA：$a\\ne b$\nB：$x\\to 0$"
