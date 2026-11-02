"""工艺流程图渲染接口：结构化描述 → SVG。

和 TikZ / 结构式一样放服务端渲染，保证「编辑器预览」与「PDF / DOCX 导出」
拿到的是同一份图。描述不合法（步数太少、JSON 坏了）直接 400 并说明原因。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from ..render import flow
from ..render.svg import png_url_for, sanitize_svg

router = APIRouter(prefix="/api/flow", tags=["flow"])


@router.post("/render")
async def render_flow(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("flow")
    warns: list[str] = []
    try:
        svg = await run_in_threadpool(flow.render, raw, True, warns)
    except flow.FlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    safe = sanitize_svg(svg)
    if not safe:
        raise HTTPException(status_code=400, detail="流程图未通过安全校验")
    result: dict[str, Any] = {"svg": safe, "spec": flow.normalise(raw)}
    img = png_url_for(safe)
    if img:
        result["image_url"], result["image_width"], result["image_height"] = img
    if warns:
        # 某几步的结构式没画出来也要说清楚，不然用户以为画上了
        result["warnings"] = warns
    return result


@router.get("/health")
async def flow_health() -> dict[str, Any]:
    return {"available": True}
