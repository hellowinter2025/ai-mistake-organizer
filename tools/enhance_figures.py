"""存量题图批量重新增强：从原始截图 + 裁剪框重算，覆盖旧的低质量版本。

为什么不是"把现有 figure_crop 再增强一遍"：增强是非线性链路（纸底归一会继续
压白、去噪会反复抹平），对结果再跑一次只会越跑越糊。所以统一从
``crop_path``（原始题目截图）+ ``figure_box`` 重新裁剪、重新增强。

用法：
    python tools/enhance_figures.py            # 全部有裁剪框的题
    python tools/enhance_figures.py --all      # 连没裁剪框的也处理（贴整块截图时）
    python tools/enhance_figures.py --dry      # 只看会改哪些题
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.routes.questions import ENHANCE_VER, _render_figure_crop  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="连没有裁剪框的题一起处理")
    ap.add_argument("--dry", action="store_true", help="只打印，不写库")
    args = ap.parse_args()

    rows = db.query(
        "SELECT id, uid, figure_crop, figure_box, crop_path FROM questions "
        "WHERE deleted=0 AND crop_path IS NOT NULL"
    )
    targets = []
    for r in rows:
        box = r["figure_box"]
        if not box and not args.all:
            continue
        try:
            box = json.loads(str(box)) if box else [0.0, 0.0, 1.0, 1.0]
        except json.JSONDecodeError:
            continue
        targets.append((r, box))

    print(f"算法版本 v{ENHANCE_VER}；待处理 {len(targets)} 道（共 {len(rows)} 道有截图）")
    done = 0
    for r, box in targets:
        old = r["figure_crop"]
        uid = r["uid"][:8]
        if args.dry:
            print(f"  · {uid} box={box}")
            continue
        target = _render_figure_crop(dict(r), box)
        if target is None:
            print(f"  ! {uid} 增强失败（截图缺失或框太小）")
            continue
        db.execute(
            "UPDATE questions SET figure_crop=? WHERE id=?", (str(target), int(r["id"]))
        )
        if old and old != str(target):
            try:
                p = Path(str(old))
                if p.exists() and p.parent == target.parent:
                    p.unlink()
            except OSError:
                pass
        done += 1
        print(f"  ✓ {uid} → {target.name}")

    if not args.dry:
        print(f"完成 {done} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
