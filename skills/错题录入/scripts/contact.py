#!/usr/bin/env python3
"""把原卷上的若干局部（批改痕迹、疑难符号、题图）拼成一张带编号的放大联系表。

一次看图覆盖所有疑点，避免「看不清 → 再裁 → 再放大」来回折腾。

用法：
  # 单张原图 + 多个区域
  python contact.py --image IMG --box 0.70,0.09,0.98,0.17 --box 0.35,0.41,0.80,0.48 \
      --out marks.png --cols 2

  # 多张原图 + 自定义标签（推荐，批量时用）
  python contact.py --spec spec.json --out marks.png --cols 2 --width 780

spec.json:
  [
    {"image": "D:/卷子/1.png", "box": [0.70,0.09,0.98,0.17], "label": "p1 q7 mark"},
    {"image": "D:/卷子/1.png", "box": [0.55,0.23,0.83,0.40], "label": "p1 q8 figure"}
  ]

box 是相对整图的 0~1 比例 [x1,y1,x2,y2]，也可写成 "x1,y1,x2,y2"。
依赖：Pillow（用项目 .venv 的 python 跑）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def parse_box(value) -> tuple[float, float, float, float]:
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    else:
        parts = list(value)
    if len(parts) != 4:
        raise ValueError(f"box 需要 4 个数，得到 {value!r}")
    x1, y1, x2, y2 = (float(p) for p in parts)
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError(f"box 必须是 0~1 且 x1<x2、y1<y2，得到 {value!r}")
    return x1, y1, x2, y2


def load_font(size: int):
    for name in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                 "/System/Library/Fonts/PingFang.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_items(args) -> list[dict]:
    items: list[dict] = []
    if args.spec:
        raw = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("items", [])
        for i, it in enumerate(raw, 1):
            items.append({
                "image": it["image"],
                "box": parse_box(it["box"]),
                "label": str(it.get("label") or i),
            })
    if args.image:
        boxes = [parse_box(b) for b in args.box]
        # 标签不足时用序号补齐
        labels = list(args.label)
        while len(labels) < len(boxes):
            labels.append(str(len(labels) + 1))
        for b, lab in zip(boxes, labels):
            items.append({"image": args.image, "box": b, "label": lab})
    if not items:
        raise SystemExit("没有可拼的区域：给 --spec 或 --image + --box")
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="原卷局部放大联系表")
    ap.add_argument("--image", help="单张原图路径")
    ap.add_argument("--box", action="append", default=[], help="0~1 区域，可重复")
    ap.add_argument("--label", action="append", default=[], help="与 --box 一一对应的标签，可重复")
    ap.add_argument("--spec", help="JSON 文件，多图多区域")
    ap.add_argument("--out", required=True, help="输出 PNG 路径")
    ap.add_argument("--cols", type=int, default=2, help="列数，默认 2")
    ap.add_argument("--width", type=int, default=780, help="每格宽度，默认 780px")
    args = ap.parse_args()

    items = build_items(args)
    font = load_font(20)
    cache: dict[str, Image.Image] = {}
    tiles: list[Image.Image] = []
    for i, it in enumerate(items, 1):
        path = str(Path(it["image"]))
        if path not in cache:
            cache[path] = Image.open(path).convert("RGB")
        im = cache[path]
        W, H = im.size
        x1, y1, x2, y2 = it["box"]
        crop = im.crop((int(x1 * W), int(y1 * H), int(x2 * W), int(y2 * H)))
        h = max(1, round(crop.height * args.width / crop.width))
        tile = crop.resize((args.width, h), Image.LANCZOS)
        d = ImageDraw.Draw(tile)
        label = f"{i}. {it['label']}"
        tw = int(d.textlength(label, font=font)) + 16
        d.rectangle((0, 0, tw, 30), fill=(255, 235, 60))
        d.text((8, 4), label, fill=(0, 0, 0), font=font)
        tiles.append(tile)

    cols = max(1, args.cols)
    row_heights = [max(t.height for t in tiles[r:r + cols]) for r in range(0, len(tiles), cols)]
    sheet = Image.new("RGB", (cols * args.width, sum(row_heights)), (255, 255, 255))
    y = 0
    for r, rh in enumerate(row_heights):
        for k in range(cols):
            idx = r * cols + k
            if idx < len(tiles):
                sheet.paste(tiles[idx], (k * args.width, y))
        y += rh

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"联系表：{out}  {sheet.size[0]}x{sheet.size[1]}px  {len(tiles)} 格")
    for i, it in enumerate(items, 1):
        print(f"  {i}. {it['label']}  {it['box']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
