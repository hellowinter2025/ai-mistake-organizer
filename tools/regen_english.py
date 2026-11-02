"""重新生成英语题：按新版「挖空」提示词重跑今天的英语题。

用户要求：英语题的题干里出错的位置要用 _____ 空出来（不能给完整句子），
答案填正确形式，语法点照旧。提示词已改，这里把今天提交的英语题重新识别一遍。

用法：
  python tools/regen_english.py --dry           # 只列清单，不调用模型
  python tools/regen_english.py --uid 218b4393  # 只跑一道（验证效果）
  python tools/regen_english.py                 # 跑今天全部英语题
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app import db  # noqa: E402
from app.ai import client as ai_client  # noqa: E402
from app.security import encode_for_ai  # noqa: E402


def targets(uid_prefix: str = "") -> list:
    sql = "SELECT * FROM questions WHERE subject = '英语' AND date(created_at) = date('now','localtime')"
    params: tuple = ()
    if uid_prefix:
        sql = "SELECT * FROM questions WHERE subject = '英语' AND uid LIKE ?"
        params = (f"{uid_prefix}%",)
    return db.query(sql + " ORDER BY created_at", params)


async def regen_one(q: dict, idx: int, total: int) -> tuple[int, int]:
    path = Path(str(q.get("crop_path") or ""))
    if not path.exists():
        print(f"[{idx}/{total}] 跳过（缺截图）{q['uid'][:8]}")
        return 0, 0
    with Image.open(path) as im:
        payload = encode_for_ai(im.convert("RGB"), 1568, 90)
    res = await ai_client.solve_question(
        payload,
        extra="",
        config_id=None,
        model=None,
        image_id=q.get("image_id"),
        subject="英语",
    )
    f = res.data or {}
    usage = res.usage or {}
    pin = int(usage.get("prompt_tokens") or 0)
    pout = int(usage.get("completion_tokens") or 0)
    patch = {
        "stem": f.get("question_text") or q.get("stem"),
        "answer": f.get("answer") or "",
        "error_reason": f.get("error_analysis") or "",
        "solution_fix": f.get("solution_fix") or "",
        "solution_alt": f.get("solution_alt") or "",
        "knowledge_points": f.get("knowledge_points") or [],
        "question_type": f.get("question_type") or q.get("question_type"),
        "difficulty": f.get("difficulty") or q.get("difficulty"),
        "raw_ai": res.raw[:20000],
        "ai_model": res.model,
        "ai_log_id": res.log_id,
    }
    db.update_question(int(q["id"]), patch)
    stem = str(patch["stem"] or "").replace("\n", " | ")
    print(f"[{idx}/{total}] ✓ {q['uid'][:8]}  ↑{pin} ↓{pout}  {stem[:72]}")
    return pin, pout


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="", help="只处理 uid 前缀匹配的题")
    ap.add_argument("--dry", action="store_true", help="只列清单")
    args = ap.parse_args()

    rows = targets(args.uid)
    print(f"待重新生成的英语题：{len(rows)}")
    if args.dry:
        for i, q in enumerate(rows, 1):
            print(f"  {i}. {q['uid'][:8]} {str(q['stem'])[:60]!r}")
        return 0

    tin = tout = 0
    for i, q in enumerate(rows, 1):
        try:
            a, b = await regen_one(dict(q), i, len(rows))
            tin += a
            tout += b
        except Exception as exc:  # noqa: BLE001 - 单题失败不打断整批
            print(f"[{i}/{len(rows)}] ✗ {q['uid'][:8]} {type(exc).__name__}: {exc}")
    print(f"\n合计 token：输入 {tin} / 输出 {tout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
