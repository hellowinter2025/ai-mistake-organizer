"""用当前渲染管线重画题库里的 TikZ 题图（可反复跑）。

为什么要这个工具：TikZ 的 SVG 是**生成后存进数据库**的，渲染器改了（比如这次
「虚线压过重合实线」的后处理），老图不会自动变。19 道有 figure_tikz 的题里，
立体图那种「底面 --cycle 画实线 + 再补虚线」的写法以前一律渲染成全实线。

用法：
    python tools/rerender_tikz.py            # 只体检，列出会变哪些
    python tools/rerender_tikz.py --apply    # 写回数据库
    python tools/rerender_tikz.py --apply --force   # 即使一样也重写
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.render import tikz  # noqa: E402
from app.render.svg import sanitize_svg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回数据库（默认只体检）")
    ap.add_argument("--force", action="store_true", help="图没变化也重写")
    args = ap.parse_args()

    rows = [
        dict(r)
        for r in db.query(
            "SELECT id, uid, code, figure_tikz, figure_svg FROM questions "
            "WHERE deleted=0 AND figure_tikz IS NOT NULL AND figure_tikz <> '' ORDER BY id"
        )
    ]
    print(f"检查 {len(rows)} 道有 TikZ 的题（{'写回' if args.apply else '只体检'}）")
    changed = failed = 0
    for row in rows:
        old = row.get("figure_svg") or ""
        try:
            result = tikz.render(row["figure_tikz"])
        except tikz.TikzError as exc:
            failed += 1
            print(f"  ! id={row['id']} #{row.get('code') or '?'} 编译失败：{exc}")
            continue
        svg = sanitize_svg(result.svg)
        if not svg:
            failed += 1
            print(f"  ! id={row['id']} #{row.get('code') or '?'} 未通过安全校验")
            continue
        same = svg == old
        if same and not args.force:
            continue
        changed += 1
        print(f"  · id={row['id']} #{row.get('code') or '?'} {'图有更新' if not same else '强制重写'}")
        if args.apply:
            db.update_question(int(row["id"]), {"figure_svg": svg})

    print(f"\n重画 {changed} 张，失败 {failed} 张")
    if not args.apply:
        print("（体检模式，没有写库；加 --apply 生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
