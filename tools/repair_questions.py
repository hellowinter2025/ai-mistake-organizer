"""修复题库里被"模型硬折行"污染的旧题。

用法：
    python tools/repair_questions.py            # 只体检，打印会改什么
    python tools/repair_questions.py --apply    # 真正写回数据库

两类处理：
1. 题干是整段原始 JSON（解析降级的产物）→ 用修好的解析器重新解析；
2. 普通文本 → 逐字段跑 ``normalise_latex``（收紧公式内空格、还原换行）。
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.ai import parser  # noqa: E402

TEXT_FIELDS = ("stem", "answer", "error_reason", "solution_fix", "solution_alt", "notes")
LOOKS_LIKE_RAW_JSON = ("{", "[[[")


def looks_raw(stem: str) -> bool:
    head = (stem or "").lstrip()
    return head.startswith("{") or head.startswith(LOOKS_LIKE_RAW_JSON)


def repair_row(row: dict) -> tuple[dict, str]:
    """返回 (补丁, 采取的策略)。空补丁表示无需改动。"""
    stem = row.get("stem") or ""
    patch: dict = {}

    if looks_raw(stem):
        try:
            res = parser.parse(stem)
        except parser.ParseError as exc:
            return {}, f"无法解析：{exc.detail or exc}"
        if res.data.get("question_text"):
            patch["stem"] = res.data["question_text"]
            for src, dst in (
                ("answer", "answer"),
                ("error_analysis", "error_reason"),
                ("solution_fix", "solution_fix"),
                ("solution_alt", "solution_alt"),
            ):
                if res.data.get(src):
                    patch[dst] = res.data[src]
            if res.data.get("knowledge_points"):
                patch["knowledge_points"] = res.data["knowledge_points"]
            if res.data.get("difficulty"):
                patch["difficulty"] = res.data["difficulty"]
            return patch, "重新解析原始 JSON"

    for field in TEXT_FIELDS:
        value = row.get(field) or ""
        if not value:
            continue
        cleaned, _warns = parser.normalise_latex(value)
        if field == "stem":
            cleaned = parser.strip_question_number(cleaned)
        if cleaned != value:
            patch[field] = cleaned
    return patch, "收紧公式与换行"


def _preview(before: str, after: str, limit: int = 220) -> str:
    diff = list(
        difflib.unified_diff(
            (before or "").splitlines(),
            (after or "").splitlines(),
            lineterm="",
            n=0,
        )
    )
    text = "\n".join(diff[2:]) or "(仅空白差异)"
    return text[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回数据库（默认只看不改）")
    args = ap.parse_args()

    db.init_db()
    rows = [dict(r) for r in db.query("SELECT * FROM questions WHERE deleted=0 ORDER BY id")]
    print(f"共 {len(rows)} 道题")
    changed = 0
    for row in rows:
        patch, how = repair_row(row)
        if not patch:
            continue
        changed += 1
        print(f"\n--- #{row['id']} {how} ---")
        for field, value in patch.items():
            if isinstance(value, str) and value != (row.get(field) or ""):
                print(f"  [{field}]")
                print("  " + _preview(row.get(field) or "", value).replace("\n", "\n  "))
        if args.apply:
            db.update_question(int(row["id"]), patch)
    print(f"\n需要修复：{changed} 道")
    if args.apply:
        print("已写回数据库")
    elif changed:
        print("加 --apply 才会真正写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
