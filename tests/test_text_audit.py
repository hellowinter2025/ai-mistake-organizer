"""题库文本体检工具的自检：能抓到脏数据，也不会误伤正常写法。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import audit_question_text as audit  # noqa: E402


def names(field: str, text: str) -> list[str]:
    return [n for n, _a, _s in audit.scan_value(field, text)]


def test_detects_literal_newline_escape():
    """模��把 JSON 多转义一层时留下的 `\\n` 必须被报出来。"""
    found = names("error_reason", "A 项正确。\\nB 项错误。")
    assert "字面量 \\n/\\r/\\t" in found


def test_keeps_tex_commands_quiet():
    """`\\ne` / `\\theta` / `\\to` 是公式，不是换行。"""
    text = "由 $a\\ne b$、$\\theta=\\angle ABC$、$x\\to 0$ 得"
    assert names("solution_fix", text) == []


def test_detects_html_and_control_chars():
    assert "HTML 残留" in names("solution_fix", "第一行<br>第二行")
    assert "控制字符" in names("solution_alt", "abc\x0bdef")


def test_choice_blank_is_not_dirty():
    """选择题的「（　　）」用全角空格占位是标准写法，不该报警。"""
    assert names("stem", "下列说法正确的是（　　）") == []


def test_unbalanced_dollar_is_reported():
    assert any("$ 定界符" in n for n in names("solution_fix", "若 $x>0 则"))


def test_fix_html_and_escapes():
    fixed = audit.fix_value("solution_fix", "甲<br>乙\\n丙", "html")
    assert fixed == "甲\n乙\\n丙"       # <br> 变真换行，字面量 \n 归 unescape 规则管
    fixed2 = audit.fix_value("solution_fix", fixed, "unescape")
    assert fixed2 == "甲\n乙\n丙"
