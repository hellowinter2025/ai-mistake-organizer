"""题图渲染接口：把 TikZ 源码编译成 SVG。

编辑器里改完 TikZ 点「渲染预览」，走这里而不是让浏览器自己编译——
前端没有 TeX 引擎，而且预览结果必须和导出 PDF / DOCX 用的是同一套渲染。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from ..render import tikz
from ..render.svg import png_url_for, sanitize_svg

router = APIRouter(prefix="/api/tikz", tags=["tikz"])


@router.post("/render")
async def render_tikz(payload: dict[str, Any]) -> dict[str, Any]:
    code = str(payload.get("code") or "")
    if not code.strip():
        raise HTTPException(status_code=400, detail="TikZ 代码为空")
    try:
        result = await run_in_threadpool(tikz.render, code)
    except tikz.TikzError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    svg = sanitize_svg(result.svg)
    if not svg:
        raise HTTPException(status_code=400, detail="编译结果未通过安全校验")
    out: dict[str, Any] = {"svg": svg, "ms": result.ms, "cached": result.cached}
    # 代码被自动补全过（截断的 \end{tikzpicture}、漏写的分号…）就回传，
    # 前端把输入框同步成能编译的版本，用户不用自己找哪里没闭合。
    if result.code and result.code.strip() != code.strip():
        out["code"] = result.code
        out["fixes"] = result.fixes
    img = png_url_for(svg)
    if img:
        out["image_url"], out["image_width"], out["image_height"] = img
    return out


@router.get("/health")
async def tikz_health() -> dict[str, Any]:
    """设置页/排错用：node 与渲染脚本是否就位。"""
    return {"available": tikz.SERVER.exists(), "server": str(tikz.SERVER)}
