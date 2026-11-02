"""题库文本体检：找「解析没处理干净」的残留。

比 scan_literal_escapes.py 宽一层 —— 那个只盯字面量 ``\\n``，这里把实际踩过的
和极可能出现的解析残留都列出来，一条命令看完题库到底有哪些脏东西：

- 字面量转义：``\\n`` ``\\r`` ``\\t``（含 ``\\\\n`` 双反斜杠、``\\u000a``）
- HTML 残留：``<br>`` ``&nbsp;`` ``<p>`` ``&lt;``
- 控制字符：真正的 VT/FF/NUL（JSON 里 \u000b 之类解出来的）
- JSON 碎片：末尾 ``","key":``、``"}``、``null``/``None``/``undefined`` 当正文
- 定界符失衡：``$`` 奇数个（KaTeX 会整段不渲染）
- 全角空格 / 零宽字符（看不见，但会让检索对不上）

用法：
    python tools/audit_question_text.py              # 只报告
    python tools/audit_question_text.py --apply      # 自动修可确定的
    python tools/audit_question_text.py --uid xxx    # 只看某一题
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ai.parser import _is_tex_command, strip_json_tail, unescape_literals  # noqa: E402

DB = ROOT / "data" / "app.db"

FIELDS = [
    "stem", "answer", "error_reason", "solution_fix", "solution_alt",
    "knowledge_points", "tags", "notes", "figure_tikz",
]

BACKSLASH = chr(92)

RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("双反斜杠转义", re.compile(r"\\\\[nrt](?![A-Za-z])"), "unescape"),
    ("unicode 转义文本", re.compile(r"\\u[0-9a-fA-F]{4}"), "unescape_u"),
    ("HTML 残留", re.compile(r"</?(?:br|p|div|span|b|i|strong|em)\s*/?>|&nbsp;|&lt;|&gt;|&amp;"), "html"),
    ("控制字符", re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]"), "ctrl"),
    ("JSON 尾巴", re.compile(r'"(?:knowledge_points|difficulty|confidence)"\s*:'), "json_tail"),
    ("裸 null 当正文", re.compile(r"(?:^|\s)(?:null|None|undefined|NaN)(?:\s|$)"), "report"),
    ("零宽字符", re.compile(r"[\u200b-\u200d\ufeff]"), "blank"),
    ("全角空格（非括号内）", re.compile(r"\u3000"), "blank"),
]

# 字面量 \n / \r / \t：不能只看「后面是不是字母」——`\nB 项错误` 后面就是字母，
# 但它确实是换行；而 `\ne`、`\to`、`\theta`、`\node` 是真命令。判据与解析层一致：
# 把整个词丢进 TeX 命令表查。
LITERAL_ESCAPE = re.compile(r"\\([nrt])")


def literal_escape_spans(text: str) -> list[int]:
    out: list[int] = []
    for m in LITERAL_ESCAPE.finditer(text):
        i = m.start() + 1
        j = i
        while j < len(text) and text[j].isascii() and text[j].isalpha():
            j += 1
        if not _is_tex_command(text[i:j]):
            out.append(m.start())
    return out

DOLLAR = re.compile(r"(?<!\\)\$")


def scan_value(field: str, text: str) -> list[tuple[str, str, str]]:
    """返回 [(规则名, 修复方式, 片段说明)]。"""
    out: list[tuple[str, str, str]] = []
    # 选择题的「（　　）」用全角空格占位是正常写法，不算脏数据
    probe = re.sub(r"[（(][\s\u3000]*[）)]", "", text)
    for pos in literal_escape_spans(text)[:1]:
        snippet = text[max(0, pos - 18): pos + 14].replace("\n", "⏎")
        out.append(("字面量 \\n/\\r/\\t", "unescape", snippet))
    for name, pattern, action in RULES:
        target = probe if name.startswith("全角空格") else text
        m = pattern.search(target)
        if not m:
            continue
        snippet = text[max(0, m.start() - 18): m.start() + 14].replace("\n", "⏎")
        out.append((name, action, snippet))
    # 公式定界符：奇数个 $ 说明有一处没闭合，KaTeX 会整段失效
    if field not in ("knowledge_points", "tags", "figure_tikz"):
        if len(DOLLAR.findall(text)) % 2 == 1:
            out.append(("$ 定界符奇数", "report", "全文共 %d 个 $" % len(DOLLAR.findall(text))))
    return out


def fix_value(field: str, text: str, action: str) -> str:
    if action == "unescape":
        return unescape_literals(text.replace("\\\\n", "\\n").replace("\\\\r", "\\r").replace("\\\\t", "\\t"))
    if action == "unescape_u":
        def repl(m: re.Match[str]) -> str:
            try:
                ch = chr(int(m.group(0)[2:], 16))
            except ValueError:
                return m.group(0)
            return ch if ch.isprintable() or ch in "\n\r\t" else m.group(0)
        return re.sub(r"\\u[0-9a-fA-F]{4}", repl, text)
    if action == "html":
        s = re.sub(r"<br\s*/?>", "\n", text)
        s = re.sub(r"</p\s*>\s*<p[^>]*>", "\n", s)
        s = re.sub(r"</?(?:p|div|span|b|i|strong|em)[^>]*>", "", s)
        return (s.replace("&nbsp;", " ").replace("&lt;", "<")
                 .replace("&gt;", ">").replace("&amp;", "&"))
    if action == "ctrl":
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if action == "json_tail":
        return strip_json_tail(text)[0]
    if action == "blank":
        return re.sub(r"[\u200b-\u200d\ufeff]", "", text).replace("\u3000", " ")
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--uid")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    sql = "select uid, code, deleted, " + ", ".join(FIELDS) + " from questions"
    params: tuple = ()
    if args.uid:
        sql += " where uid=?"
        params = (args.uid,)
    rows = db.execute(sql, params).fetchall()

    counts: dict[str, int] = {}
    changed = 0
    for row in rows:
        tag = row["code"] or row["uid"][:8]
        if row["deleted"]:
            tag += "(已删)"
        for field in FIELDS:
            value = row[field]
            if not isinstance(value, str) or not value:
                continue
            issues = scan_value(field, value)
            if not issues:
                continue
            for name, action, snippet in issues:
                counts[name] = counts.get(name, 0) + 1
                print(f"{tag:12s} {field:15s} {name:14s} {snippet}")
                if args.apply and action != "report":
                    value = fix_value(field, value, action)
            if args.apply:
                db.execute("update questions set {} = ? where uid=?".format(field), (value, row["uid"]))
                changed += 1

    if args.apply:
        db.commit()
    print("\n汇总：")
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {n}")
    print(f"  修复字段数: {changed}" if args.apply else "  （未改动，加 --apply 修可确定的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
