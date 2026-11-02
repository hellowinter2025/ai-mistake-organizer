"""修复「字段串味」：AI 输出的 JSON 尾巴跑进正文（答案 / 另解 / 解析）。

症状：一道题的「另解」结尾长这样 ——
    ……故应为 $\\frac{1}{\\sqrt{3}}$。","knowledge_points":["线面垂直的判定与性质",
    "二面角的平面角"],"difficulty":4,"confidence":0.62,"needs_figure":true
印到试卷上完全没法看，而且知识点、难度这些字段本身还丢了。

成因：整体 JSON 坏掉（常见是收尾多一个引号）后走「逐字段抢救」，
而字段头正则只认 ``"key": "`` 这种字符串值，数组 / 数字 / 布尔值的头
不算边界，于是前一个字段一路吞到文末。

现在解析器已修好，这个工具负责把**存量**数据捞回来：
1. 优先用该题原始 AI 响应（``raw_ai`` 全文，或 ``ai_logs.raw_path`` 指向的日志）
   重新解析，拿回干净字段；
2. 拿不到原始响应就只做截断（把 JSON 尾巴砍掉）。

用法：
    python tools/repair_field_leak.py            # 只体检
    python tools/repair_field_leak.py --apply    # 写回数据库
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.ai import parser  # noqa: E402
from app.config import LOG_DIR  # noqa: E402

TEXT_FIELDS = ("stem", "answer", "error_reason", "solution_fix", "solution_alt", "notes")


def _polluted(row: dict) -> list[str]:
    hits = []
    for key in TEXT_FIELDS:
        value = row.get(key) or ""
        if not isinstance(value, str):
            continue
        if parser.strip_json_tail(value)[1]:
            hits.append(key)
    return hits


def _raw_source(row: dict) -> str:
    """尽量取**完整**的原始响应：raw_ai 可能只是 1500 字预览。"""
    raw = str(row.get("raw_ai") or "")
    if len(raw) >= 2000:
        return raw
    log_id = row.get("ai_log_id")
    if log_id:
        hit = db.query("SELECT raw_path FROM ai_logs WHERE id=?", (log_id,))
        if hit:
            rel = hit[0]["raw_path"]
            if rel:
                path = (LOG_DIR.parent / str(rel)).resolve()
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if len(text) > len(raw):
                    return text
    return raw


def _is_garbage(value: str) -> bool:
    """整段只剩引号/括号/逗号 —— 说明这个字段被吞得只剩尾巴，清空即可。"""
    import re

    return not re.sub(r"""[\s"'}{\[\],]+""", "", value or "")


def _patch_for(row: dict) -> tuple[dict, list[str]]:
    polluted = _polluted(row)
    patch: dict = {}
    notes: list[str] = []
    parsed = None
    raw = _raw_source(row)
    if raw.strip():
        try:
            parsed = parser.parse(raw, strict=False).data
        except Exception:  # noqa: BLE001 - 原始响应坏了就只做截断
            parsed = None

    for key in polluted:
        value = row.get(key) or ""
        clean = (parsed or {}).get(key) if parsed else ""
        if not isinstance(clean, str) or not clean.strip():
            clean, _ = parser.strip_json_tail(value)
        if clean == value:
            continue
        # 清空只在「整段本来就是垃圾」时允许，别把有用内容删了
        if not clean and not _is_garbage(value):
            continue
        patch[key] = clean
        notes.append(f"{key}: 截掉了混进来的 JSON 字段（{len(value)} → {len(clean)} 字）")

    # 被吞掉的结构化字段：库里是空的时候才补，绝不覆盖用户自己改过的
    if parsed:
        if not (row.get("knowledge_points") or "[]").strip("[] ") and parsed.get("knowledge_points"):
            patch["knowledge_points"] = __import__("json").dumps(
                parsed["knowledge_points"], ensure_ascii=False
            )
            notes.append(f"knowledge_points: 补回 {len(parsed['knowledge_points'])} 个知识点")
        if row.get("difficulty") in (None, "", 0) and parsed.get("difficulty"):
            patch["difficulty"] = parsed["difficulty"]
            notes.append(f"difficulty: 补回 {parsed['difficulty']}")
    return patch, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回数据库（默认只体检）")
    args = ap.parse_args()

    rows = [dict(r) for r in db.query("SELECT * FROM questions WHERE deleted=0 ORDER BY id")]
    print(f"检查 {len(rows)} 道题（{'写回' if args.apply else '只体检'}）")
    changed = 0
    for row in rows:
        patch, notes = _patch_for(row)
        if not notes:
            continue
        changed += bool(patch)
        print(f"  · id={row['id']} {str(row['uid'])[:8]}")
        for n in notes:
            print(f"      - {n}")
        if patch and args.apply:
            db.update_question(int(row["id"]), patch)

    print(f"\n受影响题目：{changed} 道")
    if not args.apply:
        print("（体检模式，没有写库；加 --apply 生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
