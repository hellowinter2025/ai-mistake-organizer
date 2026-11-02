"""图片上传、缩略图、切题分析。"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .. import db, imaging
from ..config import MAX_IMAGES_PER_UPLOAD, settings
from ..security import UploadError, load_upload
from . import common

router = APIRouter(prefix="/api", tags=["images"])


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
