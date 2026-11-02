"""AI 解析接口。"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from .. import db, imaging
from ..ai import client as ai_client
from ..ai import prompts as prompt_lib
from ..config import settings
from ..security import encode_for_ai
from . import common

router = APIRouter(prefix="/api/ai", tags=["ai"])

_sem: asyncio.Semaphore | None = None
_sem_limit = 0


def _semaphore() -> asyncio.Semaphore:
    """AI 请求的并发闸门；**并发数改了立即生效**。

    以前只建一次并缓存，用户在设置里把并发从 2 调到 4 后，服务端仍是 2 ——
    前端开 4 个 worker，两个一直卡在等待上（"不断等待"就是这么来的）。
    """
    global _sem, _sem_limit
    limit = max(1, min(8, int(settings.get("ai", "concurrency", default=2))))
    if _sem is None or limit != _sem_limit:
        _sem = asyncio.Semaphore(limit)
        _sem_limit = limit
    return _sem


@router.post("/solve")
async def solve(payload: dict[str, Any]) -> dict[str, Any]:
    image_id = str(payload.get("image_id") or "")
    bbox = payload.get("bbox")
    if not image_id:
        raise HTTPException(status_code=400, detail="缺少 image_id")
    if not bbox:
        raise HTTPException(status_code=400, detail="缺少选区 bbox")

    box = bbox if isinstance(bbox, (list, tuple)) else [bbox.get(k) for k in ("x", "y", "w", "h")]
    try:
        box = [float(v) for v in box]
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="bbox 格式错误") from exc
    if box[2] < 20 or box[3] < 12:
        raise HTTPException(status_code=400, detail="选区太小，请重新框选")

    try:
        img = await run_in_threadpool(common.load_image, image_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc

    try:
        crop_img = await run_in_threadpool(imaging.crop, img, box)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = common.new_crop_token(image_id)
    await run_in_threadpool(common.save_crop, crop_img, token)
    ai_bytes = await run_in_threadpool(encode_for_ai, crop_img, 1568, 90)

    extra = str(payload.get("extra") or "").strip()[:500]
    subject = prompt_lib.normalise_subject(
        payload.get("subject") or settings.get("general", "subject", default="数学")
    )
    async with _semaphore():
        try:
            result = await ai_client.solve_question(
                ai_bytes,
                extra=extra,
                config_id=payload.get("config_id"),
                model=payload.get("model"),
                image_id=image_id,
                subject=subject,
            )
        except ai_client.AIError as exc:
            # 422 = 模型答了但内容不能用（解析失败 / 缺题干），与 502 网络/上游故障区分开
            code = exc.status if exc.status in (400, 413, 422, 429) else 502
            detail = str(exc)
            if code == 422:
                detail += "。原始响应已存档，可在右上角设置里查看最近调用记录后重试。"
            raise HTTPException(status_code=code, detail=detail) from exc

    return {
        "fields": result.data,
        "subject": subject,
        "meta": {
            "model": result.model,
            "config": result.config_name,
            "strategy": result.strategy,
            "degraded": result.degraded,
            "warnings": result.warnings,
            "latency_ms": result.latency_ms,
            "mode": result.mode,
            "log_id": result.log_id,
            "raw_preview": result.raw[:1500],
            "usage": result.usage,
        },
        "crop": {
            "token": token,
            "url": common.crop_url(token),
            "width": crop_img.width,
            "height": crop_img.height,
        },
        "bbox": box,
    }


@router.get("/logs")
async def logs(limit: int = 40) -> dict[str, Any]:
    items = db.list_logs(max(1, min(200, limit)))
    stats_row = db.query_one(
        "SELECT COUNT(*) AS total, SUM(ok) AS ok, AVG(latency_ms) AS avg_ms, "
        "SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out FROM ai_logs "
        "WHERE created_at >= datetime('now','-7 day','localtime')"
    )
    total = int(stats_row["total"] or 0) if stats_row else 0
    ok = int(stats_row["ok"] or 0) if stats_row else 0
    avg_ms = int(stats_row["avg_ms"] or 0) if stats_row else 0
    tokens_in = int(stats_row["tokens_in"] or 0) if stats_row else 0
    tokens_out = int(stats_row["tokens_out"] or 0) if stats_row else 0
    all_row = db.query_one(
        "SELECT COUNT(*) AS total, SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out FROM ai_logs"
    )
    return {
        "items": items,
        "week": {"total": total, "ok": ok, "avg_ms": avg_ms,
                 "success_rate": round(ok / total, 3) if total else None,
                 "tokens_in": tokens_in, "tokens_out": tokens_out},
        "all": {
            "total": int(all_row["total"] or 0) if all_row else 0,
            "tokens_in": int(all_row["tokens_in"] or 0) if all_row else 0,
            "tokens_out": int(all_row["tokens_out"] or 0) if all_row else 0,
        },
    }


@router.get("/prompt")
async def prompt_preview(json_mode: bool = True, subject: str = "") -> dict[str, Any]:
    subject = prompt_lib.normalise_subject(subject or settings.get("general", "subject", default="数学"))
    return {
        "system": prompt_lib.system_prompt(json_mode, subject),
        "subject": subject,
        "latex": prompt_lib.uses_latex(subject),
    }
