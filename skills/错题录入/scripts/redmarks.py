#!/usr/bin/env python3
"""把红笔批改从照片里分离出来，逐处裁成小图 —— 判「哪几个空是真错」的前置步骤。

为什么需要它：语法填空/短文改错的原始照片上，学生的答案和老师的红笔混在一起，
靠肉眼按比例估坐标去裁，误差常常一两厘米，裁出来的常常是隔壁那一空。
这里直接按颜色把红笔抠出来，再按行聚成簇，每簇向外扩一圈上下文再裁，
一簇就是一处订正，读起来不会张冠李戴。

判据：红笔的红色通道明显高于绿蓝通道，且不像纸底那样亮。铅笔/中性笔的
黑色字迹被排除，红色印刷小标题（如「四、语法填空」）可能残留一点，忽略即可。

用法：
    python redmarks.py mask  <图片路径> --out 掩膜.jpg
    python redmarks.py spans <图片路径> --outdir 目录 [--min-height 14]

输出：mask 给一张只剩红笔的图（一眼看出有几处批改）；spans 给一组小图，
每张都以某处红笔为中心、带足够上下文，直接看图就能读出「学生原本写的是什
么、老师改成什么」。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:  # Windows 控制台默认 GBK，中文路径会抛 UnicodeEncodeError
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

try:
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：pip install pillow numpy")


def red_mask(path: Path) -> "np.ndarray":
    """返回布尔掩膜：True = 红笔。"""
    arr = np.asarray(Image.open(path).convert("RGB")).astype(np.int16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    # 纸底偏亮偏黄，红笔饱和度高；用「红比绿蓝均值高出多少」来区分
    return (r - (g + b) / 2 > 30) & (r > 80)


def cmd_mask(path: Path, out: Path) -> None:
    mask = red_mask(path)
    out_arr = np.full((*mask.shape, 3), 255, dtype=np.uint8)
    out_arr[mask] = (200, 0, 0)
    img = Image.fromarray(out_arr)
    img.thumbnail((1400, 1900), Image.LANCZOS)  # 太大会被看图工具压掉细节
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=90)
    print(f"红笔像素 {int(mask.sum())} 个 → {out}")


def cmd_spans(path: Path, outdir: Path, min_height: int) -> None:
    mask = red_mask(path)
    im = Image.open(path).convert("RGB")
    width, height = im.size

    rows = mask.sum(axis=1)
    spans: list[list[int]] = []
    cur: list[int] | None = None
    for y, count in enumerate(rows):
        if count > 2:
            cur = [y, y] if cur is None else [cur[0], y]
        elif cur is not None:
            spans.append(cur)
            cur = None
    if cur:
        spans.append(cur)

    # 同一行字常被拆成上下两段（比如「g」的下伸部），挨得近就合并
    merged: list[list[int]] = []
    for s in spans:
        if merged and s[0] - merged[-1][1] < 18:
            merged[-1][1] = s[1]
        else:
            merged.append(list(s))

    outdir.mkdir(parents=True, exist_ok=True)
    made = 0
    for y0, y1 in merged:
        if y1 - y0 < min_height:  # 细线多半是大勾、加大叉的笔画，不是订正词
            continue
        band = mask[y0:y1 + 1]
        xs = np.where(band.sum(axis=0) > 0)[0]
        if xs.size == 0:
            continue
        x0, x1 = int(xs.min()), int(xs.max())
        # 左右各留 420px（约一行字长）才能看到「哪一空」，上下留一点避免切掉笔画
        box = (max(0, x0 - 420), max(0, y0 - 26), min(width, x1 + 420), min(height, y1 + 26))
        crop = im.crop(box)
        scale = min(3.0, 1600 / max(1, crop.width))
        if scale > 1:
            crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)
        dest = outdir / f"red_{made:02d}.jpg"
        crop.save(dest, quality=94)
        print(f"red_{made:02d}.jpg  y={y0}-{y1}  x={x0}-{x1}")
        made += 1
    print(f"共 {made} 处红笔 → {outdir}")
    if made == 0:
        print("没有红笔痕迹：这份可能是没批改的，或拍照偏色太重，换个角度重拍再试")


def main() -> int:
    ap = argparse.ArgumentParser(description="分离红笔批改痕迹")
    ap.add_argument("mode", choices=("mask", "spans"))
    ap.add_argument("image")
    ap.add_argument("--out", help="mask 模式的输出图")
    ap.add_argument("--outdir", help="spans 模式的输出目录")
    ap.add_argument("--min-height", type=int, default=14, help="小于这个高度的红笔行忽略")
    args = ap.parse_args()

    path = Path(args.image).expanduser()
    if not path.exists():
        sys.exit(f"找不到图片：{path}")
    if args.mode == "mask":
        out = Path(args.out) if args.out else path.with_name(path.stem + "_red.jpg")
        cmd_mask(path, out)
    else:
        outdir = Path(args.outdir) if args.outdir else path.with_name(path.stem + "_red")
        cmd_spans(path, outdir, args.min_height)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
