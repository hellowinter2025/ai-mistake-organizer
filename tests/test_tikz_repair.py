"""TikZ 补全：模型输出被截断/写坏时，渲染层要能救回来。"""
from __future__ import annotations

import pytest

from app.render.tikz import TikzError, repair_environment


def test_closed_code_is_untouched():
    code = "\\begin{tikzpicture}\n\\draw (0,0)--(1,1);\n\\end{tikzpicture}"
    assert repair_environment(code) == (code, [])


def test_truncated_end_tag_is_closed():
    """真实踩到过：输出被 max_tokens 截断，末尾是 \\end{tikzpicture（少个花括号）。"""
    code = "\\begin{tikzpicture}[scale=0.75]\n\\coordinate (B) at (0,0);\n\\end{tikzpicture"
    fixed, notes = repair_environment(code)
    assert fixed.endswith("\\end{tikzpicture}")
    assert "\\coordinate (B) at (0,0);" in fixed
    assert notes, "要说明补了什么"


def test_half_written_statement_is_dropped():
    """半句 \\draw (A)--(B 会让整个 TeX 编译失败，只能切掉最后一句。"""
    code = "\\begin{tikzpicture}\n\\draw (0,0)--(1,1);\n\\draw (A)--(B"
    fixed, notes = repair_environment(code)
    assert "\\draw (0,0)--(1,1);" in fixed
    assert "\\draw (A)--(B\n" not in fixed
    assert fixed.endswith("\\end{tikzpicture}")
    assert any("没写完" in n for n in notes)


def test_missing_semicolon_is_added():
    code = "\\begin{tikzpicture}\n\\draw (0,0)--(1,1)\n\\end{tikzpicture}"
    fixed, notes = repair_environment(code)
    assert "\\draw (0,0)--(1,1);" in fixed
    assert any("分号" in n for n in notes)


def test_unclosed_options_are_closed():
    code = "\\begin{tikzpicture}[scale=0.6\n\\draw (0,0)--(1,1);"
    fixed, _ = repair_environment(code)
    assert fixed.startswith("\\begin{tikzpicture}[scale=0.6]")


def test_only_commands_gets_environment():
    code = "\\draw (0,0)--(1,1);"
    fixed, notes = repair_environment(code)
    assert fixed.startswith("\\begin{tikzpicture}")
    assert fixed.endswith("\\end{tikzpicture}")
    assert notes


def test_dollar_wrapping_is_stripped():
    code = "$$\n\\begin{tikzpicture}\n\\draw (0,0)--(1,1);\n\\end{tikzpicture}\n$$"
    fixed, _ = repair_environment(code)
    assert fixed.startswith("\\begin{tikzpicture}")
    assert not fixed.startswith("$")


def test_chinese_is_removed():
    """TeX 引擎没中文字体，一个汉字就是 inputenc Error，整图作废。"""
    code = "\\begin{tikzpicture}\n\\node at (0,0) {脱硫塔};\n\\end{tikzpicture}"
    fixed, notes = repair_environment(code)
    assert "脱硫塔" not in fixed
    assert any("汉字" in n for n in notes)


def test_options_on_next_line_are_joined():
    code = "\\begin{tikzpicture}\n[scale=0.75]\n\\draw (0,0)--(1,1);\n\\end{tikzpicture}"
    fixed, _ = repair_environment(code)
    assert fixed.startswith("\\begin{tikzpicture}[scale=0.75]")


def test_not_tikz_raises():
    with pytest.raises(TikzError):
        repair_environment("这是一段普通文字")


def test_banned_command_still_raises():
    code = "\\begin{tikzpicture}\\input{/etc/passwd}\\end{tikzpicture}"
    with pytest.raises(TikzError):
        repair_environment(code)
