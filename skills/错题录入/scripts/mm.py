#!/usr/bin/env python3
"""错题录入助手：上传 → 预览 → 裁剪 → 入库 → 回读。

设计原则：**不调用任何模型**。
图片内容由 agent 自己读（多模态直接看图），本脚本只负责把本机文件、
裁剪框和字段安全地送到本地服务（默认 127.0.0.1:8765）。因此
`POST /api/ai/solve` 这条「裁图 + 调模型」的接口这里刻意不用，
只走新增的 `POST /api/images/{id}/crop`（纯本地裁剪）。

典型流程（见 SKILL.md）：
    mm.py upload ~/Desktop/错题/*.jpg
    mm.py preview <image_id> --out %TEMP%/p1.png      # 看整图 + 自动候选编号
    mm.py crop <image_id> --span 0.04:0.33 --span 0.33:0.58
    mm.py add --json batch.json                       # 校验 + 查重 + 入库
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:  # Windows 控制台默认 GBK，中文 + 数学符号会直接抛 UnicodeEncodeError
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("缺少 httpx：请用项目 venv 的 python 运行（.venv/Scripts/python.exe）")

DEFAULT_URL = "http://127.0.0.1:8765"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
UPLOAD_BATCH = 8  # 服务端单次上限 MAX_IMAGES_PER_UPLOAD，留点余量

SUBJECTS = ("数学", "物理", "化学", "生物", "语文", "英语", "历史", "地理", "政治")
QUESTION_TYPES = ("选择题（单选）", "选择题（多选）", "填空题", "解答题", "证明题", "其他")
CHOICE_TYPES = {"选择题（单选）", "选择题（多选）"}
# 早期数据里题型只写「选择题」，判断不了单/多选，校验时放过、只提醒规范化
LEGACY_TYPES = {"选择题"}
METHOD_SUBJECTS = {"数学", "物理"}
TEXT_FIELDS = ("stem", "answer", "error_reason", "solution_fix", "solution_alt", "notes")
LIST_FIELDS = ("knowledge_points", "tags")
FIELDS = (
    "subject",
    "question_type",
    "stem",
    "answer",
    "error_reason",
    "solution_fix",
    "solution_alt",
    "knowledge_points",
    "difficulty",
    "tags",
    "notes",
    "needs_figure",
    # 「让引擎自己画」的三个源字段——不计入的话，脚本根本没法提交重绘的图，
    # 只能一路 needs_figure=1 贴截图（AGX2PK 就是这么退化的）
    "figure_tikz",
    "figure_smiles",
    "figure_flow",
)

# 给了这几个字段就等于「图由代码画」，不该再退回原题截图
FIGURE_FIELDS = ("figure_tikz", "figure_smiles", "figure_flow")

# TeX 里出现这些一定会在我们的引擎上炸（没有 shell-escape、没有外部文件）
TIKZ_BANNED = (
    "\\documentclass",
    "\\usepackage",
    "\\begin{document}",
    "\\input",
    "\\include",
    "\\usepgfplotslibrary{fillbetween}",
)

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# ---------------------------------------------------------------- 基础工具


def local_root_hint() -> Path | None:
    """本机安装（skill 在用户目录）时，用 skill 根目录下的 ``.root`` 记住项目位置。

    这个文件是**本地配置，不进仓库**：仓库里 skill 放在 ``<项目>/skills/`` 下，
    靠脚本相对位置就能找到项目根，不需要它。
    """
    hint = Path(__file__).resolve().parents[1] / ".root"
    try:
        text = hint.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text).expanduser() if text else None


def resolve_root(arg: str | None) -> Path:
    """按优先级找项目根：显式参数 → 环境变量 → 脚本相对位置 → 本地 .root。

    第 3 条让 skill 放在仓库里（``<项目>/skills/错题录入/scripts/mm.py``）时
    开箱即用，不依赖任何绝对路径；装在本机用户目录时落到第 4 条。
    """
    for cand in (arg, os.environ.get("MM_ROOT")):
        if cand:
            path = Path(cand).expanduser()
            if (path / "data").is_dir():
                return path
            sys.exit(f"--root 指向的目录不像本项目：{path}")

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "app" / "main.py").is_file() and (parent / "data").is_dir():
            return parent

    hinted = local_root_hint()
    if hinted and (hinted / "data").is_dir():
        return hinted

    sys.exit(
        "找不到项目根目录。任选一种：\n"
        "  · mm.py --root <项目根>\n"
        "  · 设环境变量 MM_ROOT=<项目根>\n"
        "  · 在本 skill 根目录写一行 .root 文件，内容为项目根路径"
    )


def read_token(root: Path) -> str:
    for name in (".session_token", "session_token"):
        path = root / "data" / name
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
    sys.exit(f"读不到访问令牌：{root / 'data' / '.session_token'}（服务启动后才会生成）")


class Api:
    """本地服务的瘦客户端。

    ``trust_env=False`` 是必须的：本机开着系统代理时，httpx 会把
    ``127.0.0.1`` 的请求也塞进代理，直接连不上（curl 要 ``--noproxy`` 同理）。
    """

    def __init__(self, base: str, root: Path) -> None:
        self.base = base.rstrip("/")
        self.root = root
        self.client = httpx.Client(
            base_url=self.base,
            timeout=httpx.Timeout(90.0, connect=5.0),
            trust_env=False,
            headers={"X-App-Token": read_token(root)},
        )

    def _check(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail") or resp.json().get("error")
            except Exception:  # noqa: BLE001
                detail = resp.text[:300]
            sys.exit(f"HTTP {resp.status_code}: {detail}")
        ctype = resp.headers.get("content-type", "")
        return resp.json() if "json" in ctype else resp.content

    def get(self, path: str, **params: Any) -> Any:
        return self._check(self.client.get(path, params=params or None))

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self._check(self.client.post(path, json=body, **kwargs))

    def patch(self, path: str, body: Any) -> Any:
        return self._check(self.client.patch(path, json=body))

    def upload(self, chunk: list[Path]) -> Any:
        files = [
            ("files", (p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "image/jpeg"))
            for p in chunk
        ]
        return self._check(self.client.post("/api/images", files=files))


def out(args: argparse.Namespace, payload: Any, human: str) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else human)


def dedup_path(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out_paths: list[Path] = []
    for p in paths:
        key = str(p.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        out_paths.append(p)
    return out_paths


def expand_paths(raw: list[str]) -> list[Path]:
    found: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in IMAGE_EXT))
        elif path.is_file():
            found.append(path)
        else:
            sys.exit(f"路径不存在：{path}")
    if not found:
        sys.exit("没有找到图片（支持 " + "、".join(sorted(IMAGE_EXT)) + "）")
    return dedup_path(found)


# ---------------------------------------------------------------- 选区解析


def span_box(spec: str, image: dict[str, Any]) -> list[float]:
    """``--span 0.04:0.33`` → 整宽、纵向区间的归一化框。

    试卷基本都是单栏纵向排版，一道题占一条横带，用纵坐标分段最省事，
    也比逐字估像素稳。
    """
    try:
        y1, y2 = (float(v) for v in spec.split(":"))
    except ValueError:
        sys.exit(f"--span 要写成 起点:终点（0~1），收到 {spec!r}")
    y1, y2 = sorted((y1, y2))
    if not (0 <= y1 < y2 <= 1):
        sys.exit(f"--span 超出 0~1 范围或起点终点相同：{spec}")
    return [0.0, y1, 1.0, y2 - y1]


def xrange_box(spec: str, image: dict[str, Any]) -> list[float]:
    try:
        x1, x2 = (float(v) for v in spec.split(":"))
    except ValueError:
        sys.exit(f"--x 要写成 起点:终点（0~1），收到 {spec!r}")
    x1, x2 = sorted((x1, x2))
    if not (0 <= x1 < x2 <= 1):
        sys.exit(f"--x 超出 0~1 范围：{spec}")
    return [x1, 0.0, x2 - x1, 1.0]


def combine_spans(spans: list[list[float]], xranges: list[list[float]]) -> list[list[float]]:
    """纵向分段 × 横向分段 → 网格框（双栏卷子一行两题也能一次框出来）。

    顺序按阅读习惯：先上后下、同行先左后右。
    """
    xs = xranges or [[0.0, 0.0, 1.0, 1.0]]  # 缺省整宽
    ys = spans or [[0.0, 0.0, 0.0, 1.0]]  # 缺省整高
    return [[x[0], y[1], x[2], y[3]] for y in ys for x in xs]


def parse_box_spec(spec: str) -> list[float]:
    parts = [p for p in re.split(r"[,\s]+", spec.strip()) if p]
    if len(parts) != 4:
        sys.exit(f"--box 要 4 个数 x,y,w,h（像素或 0~1）：{spec!r}")
    try:
        return [float(p) for p in parts]
    except ValueError:
        sys.exit(f"--box 含非数字：{spec!r}")


def pick_candidates(api: Api, image_id: str, spec: str) -> list[list[float]]:
    analysis = api.post(f"/api/images/{image_id}/analyze")
    cands = analysis.get("candidates") or []
    chosen: list[list[float]] = []
    for token in re.split(r"[,\s]+", spec.strip()):
        if not token:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        idxs = range(int(m.group(1)), int(m.group(2)) + 1) if m else [int(token)]
        for i in idxs:
            if not 1 <= i <= len(cands):
                sys.exit(f"候选编号 {i} 越界（共 {len(cands)} 个）")
            c = cands[i - 1]
            chosen.append([c["x"], c["y"], c["w"], c["h"]])
    return chosen


def resolve_boxes(api: Api, image_id: str, args: argparse.Namespace) -> list[list[float]]:
    boxes: list[list[float]] = []
    if getattr(args, "index", None):
        boxes.extend(pick_candidates(api, image_id, args.index))
    spans = [span_box(s, {}) for s in (getattr(args, "span", None) or [])]
    xrs = [xrange_box(s, {}) for s in (getattr(args, "x", None) or [])]
    if spans or xrs:
        boxes.extend(combine_spans(spans, xrs))
    for spec in getattr(args, "box", None) or []:
        boxes.append(parse_box_spec(spec))
    return boxes


# ---------------------------------------------------------------- 命令实现


def cmd_health(api: Api, args: argparse.Namespace) -> None:
    health = api.get("/api/health")
    out(args, {"url": api.base, **health}, f"服务正常：{api.base}  version={health.get('version')}")


def cmd_upload(api: Api, args: argparse.Namespace) -> None:
    paths = expand_paths(args.paths)
    images: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for i in range(0, len(paths), UPLOAD_BATCH):
        chunk = paths[i: i + UPLOAD_BATCH]
        res = api.upload(chunk)
        images.extend(res.get("images") or [])
        errors.extend(res.get("errors") or [])
        print(f"  已上传 {min(i + UPLOAD_BATCH, len(paths))}/{len(paths)}", file=sys.stderr)

    if args.json:
        print(json.dumps({"images": images, "errors": errors}, ensure_ascii=False, indent=2))
        return
    print(f"\n共 {len(images)} 张（重复上传会复用同一 image_id）：")
    for img in images:
        flag = "  [已存在]" if img.get("reused") else ""
        print(f"  {img['id']}  {img['width']}x{img['height']}  {img['name']}{flag}")
    for err in errors:
        print(f"  ! {err['name']}: {err['error']}")
    print("\n下一步：mm.py preview <image_id> --out <png路径>")


def cmd_analyze(api: Api, args: argparse.Namespace) -> None:
    res = api.post(f"/api/images/{args.image_id}/analyze")
    cands = res.get("candidates") or []
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print(f"尺寸 {res.get('width')}x{res.get('height')}，候选 {len(cands)} 块：")
    print(f"{'编号':<4}{'x':>6}{'y':>7}{'w':>7}{'h':>7}   归一化 y 区间")
    for i, c in enumerate(cands, 1):
        h = res.get("height") or 1
        print(
            f"{i:<4}{c['x']:>6}{c['y']:>7}{c['w']:>7}{c['h']:>7}   "
            f"{c['y'] / h:.3f}:{(c['y'] + c['h']) / h:.3f}"
        )
    print("\n用 mm.py crop <id> --index 1,3-4 直接按编号裁；编号不准就用 --span。")


def cmd_bands(api: Api, args: argparse.Namespace) -> None:
    """输出水平空白的精确位置，用来定 `--span` 的边界。

    试卷上的题与题之间必有空白行。自动切题给的是「内容块」，粒度要么太粗
    （整页两大块）、要么把选项行并到一起；而**空白带**是稳定且不歧义的：
    挑一条落在两题之间的空白带当边界，永远不会切掉半行字。

    墨迹由服务端用自适应阈值提取，阴影/纸纹/暗角不会被误判（脚本自己用
    裸灰度阈值试过，整张图会被判成一片墨迹，一条空白带都找不到）。
    """
    res = api.post(
        f"/api/images/{args.image_id}/bands",
        {"min_height": args.min_height, "density": args.density},
    )
    bands = res.get("bands") or []
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print(f"图高 {res.get('height')}px，找到 {len(bands)} 条 ≥{args.min_height}px 的空白带：")
    for b in bands:
        a, z = b["ratio"]
        print(f"  {a:.3f} ~ {z:.3f}   (y={b['from']}..{b['to']}，高 {b['px']}px)")
    print("\n把落在两题之间的那条空白带当边界：上一题的 --span 结束值 ≈ 它的起点，")
    print("下一题的起始值 ≈ 它的终点（也可以让两题都往里收一点，留白总是安全的）。")


def cmd_preview(api: Api, args: argparse.Namespace) -> None:
    boxes = resolve_boxes(api, args.image_id, args)
    body: dict[str, Any] = {"boxes": boxes} if boxes else {}
    if not args.no_grid:
        body["grid"] = True  # 百分比刻度尺：估 --span 全靠它
    png = api.post(f"/api/images/{args.image_id}/preview", body)
    dest = Path(args.out).expanduser() if args.out else (
        Path(tempfile.gettempdir()) / "mm_intake" / f"{args.image_id}_preview.png"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(png)
    print(f"预览图：{dest}（{len(boxes)} 个框{'（自动候选）' if not boxes else ''}）")
    if not args.no_grid:
        print("图上已叠加刻度：横线每 0.1（右侧标数值），竖线每 0.25。按它估 --span 起止值。")


def cmd_crop(api: Api, args: argparse.Namespace) -> None:
    boxes = resolve_boxes(api, args.image_id, args)
    if not boxes:
        sys.exit("没有选区：用 --span / --box / --index 指定")
    res = api.post(f"/api/images/{args.image_id}/crop", {"boxes": boxes})
    crops = res.get("crops") or []

    saved: list[Path] = []
    if args.save_dir:
        save_dir = Path(args.save_dir).expanduser()
        save_dir.mkdir(parents=True, exist_ok=True)
        for i, c in enumerate(crops, 1):
            path = save_dir / f"{args.image_id[:8]}_{i:02d}_{c['token'][:8]}.jpg"
            path.write_bytes(api.get(f"/api/crops/{c['token']}"))
            saved.append(path)

    if args.json:
        print(json.dumps({"crops": crops, "files": [str(p) for p in saved]}, ensure_ascii=False, indent=2))
        return
    for i, c in enumerate(crops, 1):
        tail = f"  → {saved[i - 1]}" if saved else ""
        print(f"{i}. token={c['token']}  {c['width']}x{c['height']}  bbox={c['bbox']}{tail}")
    print("\n下一步：看图写字段 → mm.py add --json <文件>")


def read_code_arg(src: str) -> str:
    if src == "-":
        return sys.stdin.read()
    path = Path(src).expanduser()
    if not path.is_file():
        sys.exit(f"读不到 TikZ 文件：{path}")
    return path.read_text(encoding="utf-8")


def png_stats(data: bytes) -> tuple[int, int, float]:
    """(宽, 高, 墨迹占比)。墨迹占比专门用来发现「编译成功但整幅画空」。"""
    try:
        import io

        from PIL import Image
    except ImportError:  # pragma: no cover - venv 一定带 Pillow
        return 0, 0, -1.0
    with Image.open(io.BytesIO(data)) as im:
        gray = im.convert("L")
        width, height = gray.size
        hist = gray.histogram()
    return width, height, sum(hist[:200]) / max(1, width * height)


def render_report(png: bytes, svg: str) -> tuple[list[str], dict[str, Any]]:
    """把「这张图渲染得对不对」变成一个可以判定的事实，而不是靠感觉。

    静态能查的只有形态问题（画空、过扁、没有标注），真正的几何关系对不对
    还得把 PNG 打开看——这个函数负责把该看的指标摊出来。
    """
    width, height, ink = png_stats(png)
    texts = len(re.findall(r"<text\b", svg or ""))
    paths = len(re.findall(r"<path\b", svg or ""))
    warns: list[str] = []
    if ink >= 0 and ink < 0.002:
        warns.append("几乎空白：编译成功了但图上几乎没有墨迹，八成坐标写错或线被裁掉")
    if width and height:
        ratio = width / height
        if ratio > 6:
            warns.append(f"过扁（宽高比 {ratio:.1f}）：排版时会被压成看不清的小条")
        elif ratio < 0.2:
            warns.append(f"过窄（宽高比 {ratio:.1f}）：确认是不是竖向长图")
    if texts == 0:
        warns.append("图里没有任何文字标注（点的字母、角度值都没标？）")
    if paths == 0:
        warns.append("图里没有任何路径（\\draw 没生效？）")
    info = {"width": width, "height": height, "ink": round(ink, 4), "texts": texts, "paths": paths}
    return warns, info


def cmd_render(api: Api, args: argparse.Namespace) -> None:
    """编译 TikZ → PNG → 落盘，然后**自己打开看**。

    这是「画完自检」的第一步：不下发这一步，重绘的图错了也发现不了，
    最后还是会退化成贴原题截图。
    """
    code = read_code_arg(args.source)
    if not code.strip():
        sys.exit("TikZ 代码为空")
    res = api.post("/api/tikz/render", {"code": code})
    svg = str(res.get("svg") or "")
    if "<svg" not in svg.lower():
        sys.exit("渲染返回的不是 SVG")
    url = res.get("image_url")
    if not url:
        sys.exit("服务端没给出 PNG 地址（浏览器栅格化失败，检查 Chrome/Edge 是否在）")
    png = api.get(url)

    import hashlib

    if args.out:
        dest = Path(args.out).expanduser()
    else:
        tag = hashlib.sha1(code.encode("utf-8")).hexdigest()[:8]
        dest = Path(tempfile.gettempdir()) / "mm_intake" / "figs" / f"tikz_{tag}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(png)

    warns, info = render_report(png, svg)
    fixes = res.get("fixes") or []
    payload = {
        "png": str(dest),
        **info,
        "ms": res.get("ms"),
        "cached": res.get("cached"),
        "fixes": fixes,
        "warnings": warns,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"渲染图：{dest}")
    print(
        f"  {info['width']}x{info['height']}px  墨迹 {info['ink'] * 100:.1f}%  "
        f"标注 {info['texts']} 个  路径 {info['paths']} 条  {res.get('ms')}ms"
        f"{'（缓存）' if res.get('cached') else ''}"
    )
    for fix in fixes:
        print(f"  自动修复：{fix}")
    for warn in warns:
        print(f"  ⚠ {warn}")
    print("  下一步：打开这张 PNG 看几何关系对不对（点是否落在该在的线上、虚实线有没有打架）")


def load_crop_image(api: Api, q: dict[str, Any]):
    from PIL import Image

    path = q.get("crop_path")
    if path and Path(str(path)).is_file():
        return Image.open(str(path)).convert("RGB")
    url = q.get("crop_url")
    if not url:
        sys.exit("这题没有原题截图，没法裁图（先在有截图的题上用，或用 --box 相对整页裁）")
    import io

    return Image.open(io.BytesIO(api.get(url))).convert("RGB")


def normalize_figure_box(spec: str, width: int, height: int) -> list[float]:
    """``--box`` 支持 0~1 比例或像素；统一成相对题目截图的归一化框。"""
    raw = parse_box_spec(spec)
    if max(raw) <= 1.0001:
        x, y, w, h = raw
    else:
        x, y, w, h = raw[0] / width, raw[1] / height, raw[2] / width, raw[3] / height
    x = min(max(x, 0.0), 0.98)
    y = min(max(y, 0.0), 0.98)
    w = min(max(w, 0.02), 1.0 - x)
    h = min(max(h, 0.02), 1.0 - y)
    return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]


def cmd_figure(api: Api, args: argparse.Namespace) -> None:
    """把图形区域从题目截图里**单独框出来**（画不了图时的正确做法）。

    直接贴整段题目的截图，导出时会把题干文字一起印在卷面上——和排版出来的
    题干重复。这里按归一化框只裁图形本体，先出一张预览给我核对，
    ``--apply`` 才真正写库。
    """
    q = resolve_question(api, args.key)
    if not isinstance(q, dict) or not q.get("uid"):
        sys.exit(f"没找到题目 {args.key}")
    img = load_crop_image(api, q)
    box = normalize_figure_box(args.box, img.width, img.height)
    px = (
        round(box[0] * img.width),
        round(box[1] * img.height),
        round(box[2] * img.width),
        round(box[3] * img.height),
    )

    from PIL import ImageDraw

    canvas = img.copy()
    draw = ImageDraw.Draw(canvas)
    draw.rectangle(
        [px[0], px[1], px[0] + px[2], px[1] + px[3]],
        outline=(220, 40, 40),
        width=max(2, round(min(img.size) / 240)),
    )
    dest = Path(args.out).expanduser() if args.out else (
        Path(tempfile.gettempdir()) / "mm_intake" / f"fig_box_{q.get('code') or q['uid'][:6]}.png"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, "PNG")

    print(f"# {q.get('code')}  原题截图 {img.width}x{img.height}")
    print(f"框（归一化）：{box}  即 {px[2]}x{px[3]}px @ ({px[0]},{px[1]})")
    print(f"预览：{dest}   ← 先看这张，框住的是不是只有图形本体")

    if not args.apply:
        print("确认无误后加 --apply 写库")
        return

    res = api.patch(f"/api/questions/{q['uid']}", {"figure_box": box, "needs_figure": 1})
    updated = res.get("question") or {}
    url = updated.get("figure_crop_url")
    print(f"已写入：needs_figure={updated.get('needs_figure')}  figure_crop={updated.get('figure_crop')}")
    if url:
        saved = dest.with_name(dest.stem + "_crop.png")
        saved.write_bytes(api.get(url))
        print(f"裁好的图：{saved}   ← 再确认一次：文字有没有被框进来、图形有没有被切边")


def normalise_title(text: str) -> str:
    """查重用的归一化：去空白、标点、LaTeX 定界符，只比正文骨架。"""
    stripped = re.sub(r"[\s$\\{}（）()\[\]，。、；：？！,.;:?!\"'“”‘’—\-_/|]+", "", text or "")
    return stripped.lower()


def search_probe(stem: str) -> str:
    """给 LIKE 检索找一个**在库里一定连着出现**的片段。

    坑：以前拿「去空白后的前 12 字」去搜，库里题干是带空格的
    （``已知函数 $f(x)=…``），少一个空格就不再是子串，查重直接失效。
    改成从原文里挑最长的连续中文片段，它天然不含 ``%`` / ``_`` 这两个
    LIKE 通配符，也不受公式排版影响。
    """
    best = ""
    for match in re.finditer(r"[\u4e00-\u9fa5]{4,}", stem or ""):
        if len(match.group(0)) > len(best):
            best = match.group(0)
    probe = best or re.sub(r"\s+", "", stem or "")
    return probe[:20].replace("%", "").replace("_", "")


def find_similar(api: Api, stem: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """返回 (完全重复, 高度相似)。

    完全重复 = 归一化后逐字相同；高度相似 = 前 40 字相同但后面有差异
    （可能多/少了小问），只提醒、不拦。
    """
    target = normalise_title(stem)
    if not target:
        return None, None

    pools: list[list[dict[str, Any]]] = []
    probe = search_probe(stem)
    if probe:
        pools.append(api.get("/api/questions", keyword=probe, limit=20).get("items") or [])
    if not any(pools):  # 题干以公式开头等极端情况，退一步比对最近录入的题
        pools.append(api.get("/api/questions", limit=60).get("items") or [])

    exact: dict[str, Any] | None = None
    similar: dict[str, Any] | None = None
    for pool in pools:
        for q in pool:
            other = normalise_title(q.get("stem") or "")
            if not other:
                continue
            if other == target:
                return q, None
            if similar is None and len(target) >= 40 and other[:40] == target[:40]:
                similar = q
    return exact, similar


def split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    parts = [p.strip() for p in re.split(r"[、,，;；\n]+", str(value))]
    return [p for p in parts if p]


def validate(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warns: list[str] = []

    stem = str(item.get("stem") or "").strip()
    if not stem:
        errors.append("stem 为空")
    elif len(stem) < 6:
        warns.append("stem 只有几个字，确认不是只抄了半句")

    subject = str(item.get("subject") or "").strip()
    if subject not in SUBJECTS:
        errors.append(f"subject={subject!r} 不在 {'/'.join(SUBJECTS)} 里")

    qtype = str(item.get("question_type") or "").strip()
    if qtype in LEGACY_TYPES:
        # 旧数据里只写了「选择题」，没法从字符串判断单/多选，放过但提醒补全
        warns.append("question_type=选择题 是旧写法，建议改成 选择题（单选）/选择题（多选）")
    elif qtype and qtype not in QUESTION_TYPES:
        errors.append(f"question_type={qtype!r} 不在 {'/'.join(QUESTION_TYPES)} 里")

    for key in TEXT_FIELDS:
        val = item.get(key)
        # 判据与 app/ai/parser.py::unescape_literals 一致：只有 `\n` 后面不是
        # 小写字母才是真换行；`\ne` / `\to` / `\theta` / `\nu` 是 TeX 命令，放过。
        if isinstance(val, str) and re.search(r"\\[nrt](?![a-z])", val):
            errors.append(f"{key} 里有字面量 \\n / \\r / \\t，要写成真正的换行")

    try:
        difficulty = int(item.get("difficulty") or 3)
    except (TypeError, ValueError):
        errors.append("difficulty 必须是 1~5 的整数")
        difficulty = 3
    if not 1 <= difficulty <= 5:
        errors.append(f"difficulty={difficulty} 超出 1~5")

    answer = str(item.get("answer") or "").strip()
    if not answer:
        warns.append("answer 为空（截图里没有答案时需要自己补）")
    elif qtype in CHOICE_TYPES:
        letters = re.fullmatch(r"[A-Ha-h]+", answer.replace(" ", ""))
        if not letters:
            warns.append(f"选择题 answer={answer!r} 不是纯字母")
        elif qtype.endswith("（单选）") and len(letters.group(0)) > 1:
            warns.append(f"单选却给了 {len(answer)} 个字母，确认有没有标错题型")
    elif re.fullmatch(r"[A-H]", answer) and qtype not in CHOICE_TYPES:
        warns.append(f"answer={answer!r} 像选项字母，但题型是 {qtype or '未填'}")

    if subject in METHOD_SUBJECTS:
        fix = str(item.get("solution_fix") or "").strip()
        alt = str(item.get("solution_alt") or "").strip()
        if fix and not fix.startswith("方法："):
            warns.append("solution_fix 没有以「方法：」开头")
        if not fix and not alt:
            warns.append("solution_fix 与 solution_alt 都为空（原思路走不通时应把方法写进另解）")

    if not split_list(item.get("knowledge_points")):
        warns.append("knowledge_points 为空")

    tikz = str(item.get("figure_tikz") or "")
    if tikz.strip():
        errors_t, warns_t = check_tikz(tikz)
        errors.extend(errors_t)
        warns.extend(warns_t)
        if item.get("needs_figure"):
            warns.append(
                "给了 figure_tikz 又标了 needs_figure=1：导出会贴原题截图，重绘的图白画"
            )
    elif item.get("needs_figure") and not (item.get("figure_smiles") or item.get("figure_flow")):
        warns.append("needs_figure=1 但没有 figure_box：导出会贴整段题干截图（含文字），建议框出图形区域")

    return errors, warns


def check_tikz(code: str) -> tuple[list[str], list[str]]:
    """入库前的 TikZ 静态检查——只管「必然编译失败」和「明显违反约定」。

    真正画得对不对，只能靠 ``mm.py render`` 编译出 PNG 自己看，静态检查替代不了。
    """
    errors: list[str] = []
    warns: list[str] = []

    if "\\begin{tikzpicture}" not in code:
        errors.append("figure_tikz 缺少 \\begin{tikzpicture}")
    if "\\end{tikzpicture}" not in code:
        # 引擎能补（repair_environment），但补出来的位置不一定对，还是提醒一下
        warns.append("figure_tikz 没写 \\end{tikzpicture}（引擎会补，但补的位置可能不对）")
    for bad in TIKZ_BANNED:
        if bad in code:
            errors.append(f"figure_tikz 里不能出现 {bad}（引擎不支持，必然编译失败）")
    if CJK_RE.search(code):
        warns.append("figure_tikz 里有汉字：图内不写汉字，中文说明写给题干")
    if ";" not in code:
        warns.append("figure_tikz 一条 \\draw 都没有分号，八成写错了")
    return errors, warns


def normalise_item(raw: dict[str, Any]) -> dict[str, Any]:
    """接受两种写法：扁平字段，或 ``{"fields": {...}, "crop_token": ...}``。"""
    if "fields" in raw and isinstance(raw["fields"], dict):
        fields = dict(raw["fields"])
        # 图形源字段允许写在包装层（和 crop_token 平级），别只认 fields 里面
        for key in ("image_id", "crop_token", "bbox", "figure_box", "needs_figure", *FIGURE_FIELDS):
            if key in raw and key not in fields:
                fields[key] = raw[key]
    else:
        fields = dict(raw)

    item: dict[str, Any] = {}
    for key in FIELDS:
        if key in fields:
            item[key] = fields[key]
    for key in LIST_FIELDS:
        item[key] = split_list(fields.get(key))
    if "difficulty" in item:
        try:
            item["difficulty"] = int(item["difficulty"])
        except (TypeError, ValueError):
            pass
    if "needs_figure" in item:
        item["needs_figure"] = 1 if item["needs_figure"] else 0
    for key in ("image_id", "crop_token", "bbox", "figure_box"):
        if fields.get(key):
            item[key] = fields[key]
    return item


def cmd_add(api: Api, args: argparse.Namespace) -> None:
    raw_text = sys.stdin.read() if args.json_file == "-" else Path(args.json_file).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        sys.exit(f"JSON 解析失败：{exc}")
    items = payload if isinstance(payload, list) else payload.get("questions") or [payload]

    rows: list[dict[str, Any]] = []
    for i, raw in enumerate(items, 1):
        item = normalise_item(raw)
        errors, warns = validate(item)
        label = str(item.get("stem") or "")[:34].replace("\n", " ")

        if errors:
            rows.append({"n": i, "status": "rejected", "label": label, "errors": errors, "warnings": warns})
            continue

        exact, similar = find_similar(api, str(item.get("stem") or ""))
        if exact and not args.force:
            rows.append(
                {
                    "n": i,
                    "status": "duplicate",
                    "label": label,
                    "code": exact.get("code"),
                    "uid": exact.get("uid"),
                    "warnings": warns,
                }
            )
            continue
        if similar:
            warns.append(f"与 #{similar.get('code')} 高度相似（前 40 字相同），确认是不是同一道题")

        body = {k: v for k, v in item.items() if v not in (None, "", [])}
        body["source"] = "manual"
        if args.dry_run:
            rows.append({"n": i, "status": "dry-run", "label": label, "warnings": warns, "payload": body})
            continue
        try:
            res = api.post("/api/questions", body)
        except SystemExit as exc:  # api._check 里的 sys.exit，转成这一题的失败
            rows.append({"n": i, "status": "failed", "label": label, "errors": [str(exc)]})
            continue
        q = res.get("question") or {}
        rows.append(
            {
                "n": i,
                "status": "created",
                "code": q.get("code"),
                "uid": q.get("uid"),
                "subject": q.get("subject"),
                "type": q.get("question_type"),
                "label": label,
                "warnings": warns,
            }
        )

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    created = [r for r in rows if r["status"] == "created"]
    print(f"入库 {len(created)} 道，其他 {len(rows) - len(created)} 道：\n")
    for r in rows:
        head = {
            "created": f"✅ #{r.get('code')}",
            "duplicate": f"⏭  已存在 #{r.get('code')}",
            "rejected": "❌ 校验未过",
            "failed": "❌ 接口失败",
            "dry-run": "🔍 预演",
        }[r["status"]]
        print(f"{r['n']:>2}. {head:<14} {r['label']}")
        for msg in r.get("errors") or []:
            print(f"      错误：{msg}")
        for msg in r.get("warnings") or []:
            print(f"      提醒：{msg}")
        if r["status"] == "duplicate":
            print("      （确认要再录一份就加 --force）")
    if created:
        print("\n回读：" + "、".join(f"#{r['code']}" for r in created))
        print("在页面里 Ctrl+F5 刷新即可看到（编号会印在卷面上，方便核对）。")


def resolve_question(api: Api, key: str) -> dict[str, Any]:
    """按 6 位编号或 uid 取一道题。"""
    key = key.strip()
    if re.fullmatch(r"[2-9A-HJKMNP-Z]{6}", key):  # 与 db.unique_code 的字符集一致
        res = api.get("/api/questions", keyword=key, limit=20)
        hit = next((q for q in res.get("items") or [] if q.get("code") == key), None)
        if not hit:
            sys.exit(f"没找到编号 {key}")
        return hit
    q = api.get(f"/api/questions/{key}").get("question")
    if not q:
        sys.exit(f"没找到题目 {key}")
    return q


def cmd_update(api: Api, args: argparse.Namespace) -> None:
    """改一道已有题目：只提交想改的字段，其余原样保留。

    为什么需要它：界面里逐题点开改太慢，而「AI 当初解析错了答案」这种事
    常常是批量的。传入的字段先过一遍同一套校验，再 PATCH。
    """
    q = resolve_question(api, args.key)
    raw_text = sys.stdin.read() if args.json_file == "-" else Path(args.json_file).read_text(encoding="utf-8")
    try:
        patch = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        sys.exit(f"JSON 解析失败：{exc}")
    if not isinstance(patch, dict):
        sys.exit("patch 必须是一个 JSON 对象（只写要改的字段）")

    merged = {k: q.get(k) for k in FIELDS if q.get(k) is not None}
    merged.update({k: v for k, v in patch.items() if k in FIELDS or k == "figure_box"})
    for key in LIST_FIELDS:
        if key in patch:
            merged[key] = split_list(patch[key])

    if args.verify:
        errors, warns = validate(merged)
        for msg in warns:
            print(f"  提醒：{msg}")
        if errors:
            for msg in errors:
                print(f"  错误：{msg}")
            sys.exit("校验未通过，未写入（去掉 --verify 可强制写入）")

    body = {k: v for k, v in patch.items() if k in FIELDS or k == "figure_box"}
    if args.dry_run:
        print(json.dumps({"code": q.get("code"), "would_patch": body}, ensure_ascii=False, indent=2))
        return
    res = api.patch(f"/api/questions/{q['uid']}", body)
    now = res.get("question") or {}
    print(f"已更新 #{now.get('code')}：{now.get('subject')} / {now.get('question_type')} / 答案 {now.get('answer')}")


def cmd_get(api: Api, args: argparse.Namespace) -> None:
    q = resolve_question(api, args.key)
    if args.json:
        print(json.dumps(q, ensure_ascii=False, indent=2))
        return
    print(f"#{q.get('code')}  {q.get('subject')} / {q.get('question_type')}  难度 {q.get('difficulty')}")
    print(f"题干：{(q.get('stem') or '')[:120]}")
    print(f"答案：{q.get('answer')}")
    print(f"错因：{(q.get('error_reason') or '')[:80]}")
    print(f"知识点：{'、'.join(q.get('knowledge_points') or [])}")


def cmd_search(api: Api, args: argparse.Namespace) -> None:
    res = api.get("/api/questions", keyword=args.text, limit=args.limit)
    items = res.get("items") or []
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print(f"命中 {res.get('total')} 道（显示 {len(items)}）：")
    for q in items:
        print(f"  #{q.get('code')}  {q.get('subject')}  {(q.get('stem') or '')[:40]}")


def fetch_all(api: Api, subject: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """把题库整页拉下来（接口单页上限 200，够用；不够就翻页）。"""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        res = api.get("/api/questions", limit=limit, offset=offset)
        items = res.get("items") or []
        out.extend(items)
        total = int(res.get("total") or 0)
        offset += len(items)
        if not items or offset >= total:
            break
    if subject:
        out = [q for q in out if q.get("subject") == subject]
    return out


def audit_question(q: dict[str, Any]) -> list[str]:
    """按各学科口径挑毛病——只报「明显不合格」，判断不了的不报。

    这里对应 SKILL.md 第 2 节的字段口径：数学要方法行、化学要逐项分析+方程式、
    英语要有句子翻译、贴图的要框出图形。审阅时先跑它，再人工看被点名的题。
    """
    issues: list[str] = []
    subject = str(q.get("subject") or "")
    stem = str(q.get("stem") or "")
    fix = str(q.get("solution_fix") or "")
    alt = str(q.get("solution_alt") or "")
    answer = str(q.get("answer") or "").strip()

    if not answer:
        issues.append("答案为空")
    if not (q.get("knowledge_points") or []):
        issues.append("没有知识点")
    if len(stem) < 8:
        issues.append("题干过短")

    if subject in {"数学", "物理"}:
        if not fix and not alt:
            issues.append("解析与另解都是空的")
        elif fix and not fix.startswith("方法："):
            issues.append("解析没有「方法：」开头")
        if fix and "触发点：" not in fix:
            issues.append("解析缺「触发点：」")
    elif subject == "化学":
        if not fix:
            issues.append("解析为空（化学必须逐项分析）")
        else:
            if "A" not in fix or "B" not in fix:
                issues.append("解析没有逐项分析 A/B/C/D")
            if "\\ce{" not in fix and "化学方程式" not in fix:
                issues.append("解析里没有任何化学式（\\ce{}）")
        if fix.startswith("方法："):
            issues.append("化学不该用「方法：/触发点：」格式")
    elif subject == "英语":
        # 按「条」统计，不能按整段统计：错题本要求一条一句（(1)(2)…），
        # 每句一两个空是正常的；只有**单条**里塞进三四个空，才是把整段照抄了。
        lines = [ln for ln in stem.split("\n") if ln.strip()]
        per_item = max((ln.count("_____") for ln in lines), default=0)
        if per_item > 3:
            issues.append(f"单条里挖了 {per_item} 个空，疑似整段照抄（只该录错的那几空）")
        # 一整篇语法填空通常十空上下，错三个就出三条；条数到了七条，
        # 说明基本把整篇抄进来了，等于又做了一遍练习。
        elif len(lines) > 6:
            issues.append(f"录了 {len(lines)} 条，疑似整段照抄（只该录学生答错的那几空）")
        if not fix:
            issues.append("没有句子翻译")
    if q.get("needs_figure") and not q.get("figure_box"):
        issues.append("贴了截图但没给 figure_box（导出会把题干文字一起印上去）")
    # 答案条数要和题干条数对上。原先只要答案里有换行就报警，可是错题本本来就是
    # 「(1)(2)(3) 多条」，答案必然多行 —— 那是误报；真正该查的是**条数不相等**。
    # 只对英语查：数学/物理解答题的 (1)(2) 是两个小问，答案本来就常写成
    # 一整段（先写(1)再写(2)），条数不相等并不代表出错。
    n_stem = len(re.findall(r"(?m)^\s*\((\d+)\)", stem)) or 1
    n_ans = len(re.findall(r"(?m)^\s*\((\d+)\)", answer)) if subject == "英语" else n_stem
    if n_ans and n_ans != n_stem:
        issues.append(f"答案 {n_ans} 条，题干 {n_stem} 条，序号对不上")
    elif not n_ans and "\n" in answer and n_stem > 1:
        issues.append(f"题干 {n_stem} 条，答案没按 (1)(2) 分条列出")
    return issues


def cmd_audit(api: Api, args: argparse.Namespace) -> None:
    """全库体检：列出每道题不符合口径的地方，输出可直接当工作清单用。"""
    items = fetch_all(api, args.subject)
    rows: list[dict[str, Any]] = []
    for q in items:
        issues = audit_question(q)
        if args.all or issues:
            rows.append(
                {
                    "code": q.get("code"),
                    "subject": q.get("subject"),
                    "type": q.get("question_type"),
                    "stem": (str(q.get("stem") or "")[:28]).replace("\n", " "),
                    "issues": issues,
                }
            )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print(f"检查 {len(items)} 道，全部符合口径")
        return
    print(f"检查 {len(items)} 道，{len(rows)} 道待处理：")
    for r in rows:
        print(f"\n#{r['code']}  {r['subject']} / {r['type']}  {r['stem']}")
        for issue in r["issues"]:
            print(f"    · {issue}")
    stat: dict[str, int] = {}
    for r in rows:
        stat[r["subject"]] = stat.get(r["subject"], 0) + 1
    print("\n按学科：" + "、".join(f"{k} {v}" for k, v in sorted(stat.items())))


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    # 子命令也要能写 --json（`mm.py add --json` 比 `mm.py --json add` 顺手）。
    # SUPPRESS 保证子命令没写时不把主 parser 已置的 True 覆盖回 False。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    p = argparse.ArgumentParser(
        prog="mm.py",
        description="错题录入助手（不调用模型，只做上传/裁剪/入库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  mm.py upload ~/Desktop/错题/            # 上传整个目录\n"
            "  mm.py preview <id> --out %TEMP%/p.png   # 整图 + 自动候选编号\n"
            "  mm.py crop <id> --span 0.04:0.33 --save-dir %TEMP%/mm\n"
            "  mm.py add --json batch.json --dry-run   # 先预演再入库\n"
        ),
    )
    p.add_argument("--url", default=os.environ.get("MM_URL", DEFAULT_URL))
    p.add_argument("--root", default=None, help="项目根目录（默认自动探测）")
    p.add_argument("--json", action="store_true", help="机器可读输出")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", parents=[common], help="确认服务和令牌可用").set_defaults(func=cmd_health)

    up = sub.add_parser("upload", parents=[common], help="上传图片（可传目录）")
    up.add_argument("paths", nargs="+")
    up.set_defaults(func=cmd_upload)

    an = sub.add_parser("analyze", parents=[common], help="列出自动切题候选框")
    an.add_argument("image_id")
    an.set_defaults(func=cmd_analyze)

    bd = sub.add_parser("bands", parents=[common], help="列出水平空白带位置（定 --span 边界用）")
    bd.add_argument("image_id")
    bd.add_argument("--min-height", type=int, default=10, help="只报这么高的空白带（像素）")
    bd.add_argument("--density", type=float, default=0.004, help="一行墨迹占比低于它算空行")
    bd.set_defaults(func=cmd_bands)

    def add_region_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--span", action="append", metavar="Y1:Y2", help="整宽纵向区间，0~1，可重复")
        parser.add_argument("--x", action="append", metavar="X1:X2", help="横向区间，0~1，可重复（配合 --span）")
        parser.add_argument("--box", action="append", metavar="x,y,w,h", help="像素或 0~1 的框，可重复")
        parser.add_argument("--index", metavar="1,3-5", help="用 analyze 的候选编号")

    pv = sub.add_parser("preview", parents=[common], help="画框预览图（PNG），用来核对选区")
    pv.add_argument("image_id")
    pv.add_argument("--out", help="保存路径")
    pv.add_argument("--no-grid", action="store_true", help="不要百分比刻度尺")
    add_region_args(pv)
    pv.set_defaults(func=cmd_preview)

    cr = sub.add_parser("crop", parents=[common], help="裁剪选区并返回 crop_token")
    cr.add_argument("image_id")
    cr.add_argument("--save-dir", help="把裁好的图下载到本目录，方便看图")
    add_region_args(cr)
    cr.set_defaults(func=cmd_crop)

    rd = sub.add_parser("render", parents=[common], help="编译 TikZ 成 PNG，用来自己核对画得对不对")
    rd.add_argument("source", help="TikZ 文件路径，或 - 表示 stdin")
    rd.add_argument("--out", help="PNG 保存路径")
    rd.set_defaults(func=cmd_render)

    fg = sub.add_parser("figure", parents=[common], help="把图形区域从题目截图里框出来（画不了图时用）")
    fg.add_argument("key", help="uid 或 6 位编号")
    fg.add_argument("--box", required=True, metavar="x,y,w,h", help="相对题目截图的比例或像素")
    fg.add_argument("--out", help="预览图保存路径")
    fg.add_argument("--apply", action="store_true", help="确认后写库")
    fg.set_defaults(func=cmd_figure)

    ad = sub.add_parser("add", parents=[common], help="校验 + 查重 + 入库（读 JSON）")
    ad.add_argument("json_file", help="JSON 文件路径，或 - 表示 stdin")
    ad.add_argument("--force", action="store_true", help="查重命中仍然入库")
    ad.add_argument("--dry-run", action="store_true", help="只校验不写库")
    ad.set_defaults(func=cmd_add)

    gt = sub.add_parser("get", parents=[common], help="按 uid 或 6 位编号回读")
    gt.add_argument("key")
    gt.set_defaults(func=cmd_get)

    ud = sub.add_parser("update", parents=[common], help="改一道已有题目（只写要改的字段）")
    ud.add_argument("key", help="6 位编号或 uid")
    ud.add_argument("json_file", help="patch 的 JSON 文件，或 - 表示 stdin")
    ud.add_argument("--verify", action="store_true", help="写入前跑一遍字段校验")
    ud.add_argument("--dry-run", action="store_true", help="只看会发什么，不写")
    ud.set_defaults(func=cmd_update)

    se = sub.add_parser("search", parents=[common], help="按关键词检索题库")
    se.add_argument("text")
    se.add_argument("--limit", type=int, default=10)
    se.set_defaults(func=cmd_search)

    au = sub.add_parser("audit", parents=[common], help="全库体检：列出不符合口径的题")
    au.add_argument("--subject", help="只看某一科")
    au.add_argument("--all", action="store_true", help="连合格的也列出来")
    au.set_defaults(func=cmd_audit)

    return p


def main() -> None:
    args = build_parser().parse_args()
    root = resolve_root(args.root)
    api = Api(args.url, root)
    try:
        args.func(api, args)
    except httpx.ConnectError:
        sys.exit(f"连不上 {args.url}：先启动服务（双击「启动.vbs」，或在项目目录跑 start.py）")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
