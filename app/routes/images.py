"""图片上传、缩略图、切题分析、选区裁剪。"""
from __future__ import annotations

import io
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from PIL import ImageDraw, ImageFont
from starlette.concurrency import run_in_threadpool

from .. import db, imaging
from ..config import MAX_IMAGES_PER_UPLOAD, settings
from ..security import UploadError, load_upload
from . import common

router = APIRouter(prefix="/api", tags=["images"])

# 单张图一次最多裁多少块（一道大题拆成 5 个小问也够用）
MAX_CROPS = 40
# 预览图上给框编号用的颜色，循环使用
_PREVIEW_COLORS = (
    (220, 38, 38),
    (37, 99, 235),
    (22, 163, 74),
    (217, 119, 6),
    (147, 51, 234),
    (8, 145, 178),
)


def _parse_boxes(raw: Any, width: int, height: int) -> list[list[int]]:
    """把 ``[[x,y,w,h], ...]``（像素）校验成整数框。

    也容忍 ``{"x":..,"y":..,"w":..,"h":..}`` 与 0~1 的归一化框——
    外部脚本用归一化比例更好写，服务端统一换算成像素。
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="boxes 必须是数组")
    if len(raw) > MAX_CROPS:
        raise HTTPException(status_code=400, detail=f"一次最多裁 {MAX_CROPS} 块")

    boxes: list[list[int]] = []
    for item in raw:
        if isinstance(item, dict):
            item = [item.get(k) for k in ("x", "y", "w", "h")]
        try:
            x, y, w, h = [float(v) for v in list(item)[:4]]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="裁剪框格式错误") from exc
        if not all(v == v for v in (x, y, w, h)):  # NaN
            raise HTTPException(status_code=400, detail="裁剪框包含非法数值")
        # 0~1 的小数视为归一化坐标
        if max(x, y, w, h) <= 1.0 and w > 0 and h > 0:
            x, y, w, h = x * width, y * height, w * width, h * height
        x = int(round(min(max(x, 0), width - 1)))
        y = int(round(min(max(y, 0), height - 1)))
        w = int(round(min(w, width - x)))
        h = int(round(min(h, height - y)))
        if w < 20 or h < 12:
            raise HTTPException(status_code=400, detail="选区太小，请重新框选")
        boxes.append([x, y, w, h])
    return boxes


def _label_font(size: int) -> Any:
    """尽量用系统里的粗体字给框编号，找不到就退回 PIL 默认位图字体。"""
    root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("msyhbd.ttc", "arialbd.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(str(root / name), size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1 不支持 size 参数
        return ImageFont.load_default()


def _preview_png(img, boxes: list[list[int]], grid: bool = False) -> bytes:
    """整图 + 编号框的预览图。框多时靠它把「第几号候选」和题目对上，比猜坐标可靠。

    ``grid=True`` 时叠加百分比刻度尺：横向每 10%、纵向每 25% 画一条细线并标数值。
    外部录入靠人眼在缩略图上估「这道题占哪一段」，没有刻度几乎必然偏 ——
    刻度线让 ``--span 0.18:0.34`` 这种写法有依据。
    """
    canvas = img.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    short = min(canvas.size)
    line_w = max(2, round(short / 380))
    font = _label_font(max(16, round(short / 26)))

    if grid:
        gfont = _label_font(max(14, round(short / 34)))
        tick = max(1, line_w // 2)
        for step in range(1, 10):
            y = round(canvas.height * step / 10)
            draw.line([(0, y), (canvas.width, y)], fill=(120, 190, 140), width=tick)
            draw.text((4, y + 2), f"{step / 10:.1f}", fill=(20, 120, 60), font=gfont)
        for step in range(1, 4):
            x = round(canvas.width * step / 4)
            draw.line([(x, 0), (x, canvas.height)], fill=(200, 200, 120), width=tick)
            draw.text((x + 4, 4), f"{step / 4:.2f}", fill=(140, 120, 20), font=gfont)

    for i, (x, y, w, h) in enumerate(boxes):
        color = _PREVIEW_COLORS[i % len(_PREVIEW_COLORS)]
        draw.rectangle([x, y, x + w, y + h], outline=color, width=line_w)
        label = str(i + 1)
        try:
            l, t, r, b = draw.textbbox((0, 0), label, font=font)
            tw, th = r - l, b - t
        except Exception:  # noqa: BLE001 - 老版本 Pillow 没 textbbox
            tw, th = font.getsize(label)  # type: ignore[attr-defined]
        pad = max(3, line_w * 2)
        bx = min(max(x, 0), max(0, canvas.width - tw - pad * 2))
        by = max(0, y - th - pad * 2)
        draw.rectangle([bx, by, bx + tw + pad * 2, by + th + pad * 2], fill=color)
        draw.text((bx + pad, by + pad), label, fill=(255, 255, 255), font=font)
    if max(canvas.size) > 1800:
        canvas.thumbnail((1800, 1800))
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _seg_opts(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    base = settings.get("segment", default={}) or {}
    out = {
        "gap_ratio": float(base.get("gap_ratio", 1.6)),
        "min_block_height": int(base.get("min_block_height", 26)),
        "detect_columns_flag": bool(base.get("detect_columns", True)),
    }
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key == "detect_columns":
            out["detect_columns_flag"] = bool(value)
        elif key in out:
            out[key] = type(out[key])(value)
    return out


@router.post("/images")
async def upload_images(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for upload in files[:MAX_IMAGES_PER_UPLOAD]:
        name = (upload.filename or "未命名图片").strip()[:120] or "未命名图片"
        # 逐个文件隔离失败：一张坏图不该让同批其他九张一并不成功
        try:
            raw = await upload.read()
            img, meta = await run_in_threadpool(load_upload, raw, name)
        except UploadError as exc:
            errors.append({"name": name, "error": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 - 上传流中断等
            errors.append({"name": name, "error": f"读取文件失败：{exc}"})
            continue

        try:
            existing = db.image_by_sha(meta["sha256"])
            if existing:
                image_id = existing["id"]
                stored = Path(existing["path"])
                reused = stored.exists()
                if not reused:  # 记录还在但文件被删了，用原 id 重建
                    stored = await run_in_threadpool(common.save_image, img, image_id)
            else:
                image_id = uuid.uuid4().hex
                stored = await run_in_threadpool(common.save_image, img, image_id)
                reused = False

            # 不再自动切题：自动检测的候选框总要人手改，不如直接让人自己框。
            # 想看自动结果仍可调 POST /images/{id}/analyze（工具链/排错还在用）。
            if not existing:
                db.insert_image(
                    {
                        "id": image_id,
                        "path": str(stored),
                        "orig_name": meta["orig_name"],
                        "width": img.width,
                        "height": img.height,
                        "sha256": meta["sha256"],
                        "page": None,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - 单张处理异常不应中断整批
            errors.append({"name": name, "error": f"处理失败：{exc}"})
            continue

        results.append(
            {
                "id": image_id,
                "url": common.image_url(image_id),
                "thumb": f"/api/images/{image_id}/thumb?w=520",
                "width": img.width,
                "height": img.height,
                "name": meta["orig_name"],
                "reused": reused,
                "candidates": [],
                "columns": None,
            }
        )

    for extra in files[MAX_IMAGES_PER_UPLOAD:]:
        errors.append(
            {
                "name": (extra.filename or "未命名图片")[:120],
                "error": f"单次最多 {MAX_IMAGES_PER_UPLOAD} 张，请分批上传",
            }
        )

    if not results:
        detail = "；".join(e["error"] for e in errors[:3]) or "上传失败"
        raise HTTPException(status_code=400, detail=detail)
    return {"images": results, "errors": errors, "reused": sum(1 for r in results if r["reused"])}


@router.post("/images/{image_id}/analyze")
async def reanalyse(image_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        img = common.load_image(image_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    opts = _seg_opts((payload or {}).get("options") or payload or {})
    analysis = await run_in_threadpool(lambda: imaging.analyse(img, **opts))
    return {"id": image_id, "url": common.image_url(image_id), "width": img.width, "height": img.height, **analysis}


@router.post("/images/{image_id}/preview")
async def preview_boxes(image_id: str, payload: dict[str, Any] | None = None) -> Response:
    """把裁剪框画在整图上（PNG），用于核对选区是否框对了题。

    不传 ``boxes`` 就先跑一遍自动切题，把候选框标出来——外部录入流程靠这张
    图把「候选编号」和「页面上第几题」对应起来，不用靠猜像素。
    """
    try:
        img = await run_in_threadpool(common.load_image, image_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    body = payload or {}
    raw = body.get("boxes")
    if raw is None and body.get("bbox") is not None:
        raw = [body["bbox"]]
    if raw is None:
        opts = _seg_opts(body.get("options") or {})
        analysis = await run_in_threadpool(lambda: imaging.analyse(img, **opts))
        raw = [[c["x"], c["y"], c["w"], c["h"]] for c in analysis.get("candidates", [])]
    boxes = _parse_boxes(raw, img.width, img.height)
    grid = bool(body.get("grid"))
    png = await run_in_threadpool(_preview_png, img, boxes, grid)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/images/{image_id}/crop")
async def crop_regions(image_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """按选区裁图并落盘，返回可直接入库的 crop_token。**不调用任何模型。**

    ``POST /api/ai/solve`` 是「裁图 + 调模型」打包在一起，外部录入（人或 agent
    自己读图）只用得上前一半。补上这个纯本地接口后，录入一道题的成本从
    「一次模型调用」降到「一次裁剪」，模型那步完全由调用方自己完成。

    支持一次传多个框（一张卷子多道题），返回顺序与传入一致。
    """
    try:
        img = await run_in_threadpool(common.load_image, image_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc

    body = payload or {}
    raw = body.get("boxes")
    if raw is None:
        raw = [body.get("bbox")] if body.get("bbox") is not None else []
    boxes = _parse_boxes(raw, img.width, img.height)
    if not boxes:
        raise HTTPException(status_code=400, detail="缺少选区 bbox")

    crops: list[dict[str, Any]] = []
    for box in boxes:
        try:
            crop_img = await run_in_threadpool(imaging.crop, img, box)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        token = common.new_crop_token(image_id)
        await run_in_threadpool(common.save_crop, crop_img, token)
        crops.append(
            {
                "token": token,
                "url": common.crop_url(token),
                "width": crop_img.width,
                "height": crop_img.height,
                "bbox": box,
            }
        )

    return {
        "image": {"id": image_id, "width": img.width, "height": img.height},
        "crops": crops,
    }


@router.get("/images/{image_id}/file")
async def get_image(image_id: str) -> FileResponse:
    try:
        path = common.image_path(image_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/images/{image_id}/thumb")
async def get_thumb(image_id: str, w: int = Query(480, ge=1)) -> FileResponse:
    """宽度越界由 common.thumbnail 夹到 [160, 1200]，不报 422。"""
    try:
        path = await run_in_threadpool(common.thumbnail, image_id, w)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/crops/{token}")
async def get_crop(token: str) -> FileResponse:
    try:
        path = common.crop_path(token)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="图片片段不存在") from exc
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=86400"})
