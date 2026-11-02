"""扫描数据库中真实的字面量转义残留（反斜杠 + n/r/t）。

用途：AI 有时把换行多转义一层，JSON 解出来是 ``\\`` + ``n`` 两个字符。
      前端按 Markdown 渲染时不会换行，进 KaTeX 还会直接报错。
      这里按字段逐条列出，配合 --fix 就地修正。

判据与 ``app.ai.parser.unescape_literals`` 一致：只有 ``\\n`` 后面的字母段长度
≤ 1（或后面不是字母）才算字面量；``\\notin`` / ``\\theta`` / ``\\node`` 这类
真 TeX 命令原样保留。

用法：
    python tools/scan_literal_escapes.py            # 只报告
    python tools/scan_literal_escapes.py --fix      # 就地修正
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ai.parser import _is_tex_command, unescape_literals  # noqa: E402

DB = ROOT / "data" / "app.db"

FIELDS = [
    "stem", "answer", "error_reason", "solution_fix", "solution_alt",
    "knowledge_points", "tags", "notes",
]

LITERAL = re.compile(r"\\([nrt])")


def is_literal(text: str, m: re.Match[str]) -> bool:
    """``\\ne`` / ``\\to`` / ``\\theta`` 这类真 TeX 命令不算残留。"""
    i = m.start() + 1
    j = i
    while j < len(text) and text[j].isascii() and text[j].isalpha():
        j += 1
    return not _is_tex_command(text[i:j])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "select uid, code, " + ", ".join(FIELDS) + " from questions where deleted=0"
    ).fetchall()

    total = 0
    fixed = 0
    for row in rows:
        for field in FIELDS:
            value = row[field] or ""
            if not isinstance(value, str):
                continue
            hits = [m for m in LITERAL.finditer(value) if is_literal(value, m)]
            if not hits:
                continue
            total += len(hits)
            tag = row["code"] or row["uid"]
            for m in hits[:3]:
                pos = m.start()
                print(f"{tag}  {field}  {m.group()!r}  …{value[max(0, pos - 22):pos + 10]!r}")
            if args.fix:
                new = unescape_literals(value)
                if new != value:
                    db.execute(
                        f"update questions set {field}=? where uid=?", (new, row["uid"])
                    )
                    fixed += 1

    if args.fix:
        db.commit()
    verdict = f"已修正 {fixed} 个字段" if args.fix else "未改动"
    print(f"\n共 {total} 处字面量转义，{verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
