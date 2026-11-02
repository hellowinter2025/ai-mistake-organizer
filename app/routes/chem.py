"""化学结构式渲染接口：把 SMILES 画成 SVG。

和 TikZ 一样放在服务端渲染，保证「编辑器预览」与「PDF / DOCX 导出」
用的是同一份输出。SMILES 本身有严格语法，所以这里还能顺带做一次校验：
解析不了直接 400，前端提示用户改。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from ..render import chem
from ..render.svg import png_url_for, sanitize_svg

router = APIRouter(prefix="/api/chem", tags=["chem"])


@router.post("/render")
async def render_chem(payload: dict[str, Any]) -> dict[str, Any]:
    if not chem.available():
        raise HTTPException(status_code=400, detail="未安装 RDKit，无法渲染结构式")
    problems: list[tuple[str, str]] = []
    mapping = chem.normalise(payload.get("smiles"), rejects=problems)
    if not mapping:
        raise HTTPException(status_code=400, detail=_explain(problems))
    try:
        svg = await run_in_threadpool(chem.render_svg, mapping)
    except chem.ChemError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    safe = sanitize_svg(svg)
    if not safe:
        raise HTTPException(status_code=400, detail="渲染结果未通过安全校验")
    result: dict[str, Any] = {"svg": safe, "molecules": mapping}
    img = png_url_for(safe)
    if img:
        result["image_url"], result["image_width"], result["image_height"] = img
    if problems:
        # 部分成功也要说清楚哪几个被丢了，不然用户以为全画上了
        result["warnings"] = [f"{name} 的 SMILES 无法解析：{smi[:40]}" for name, smi in problems]
    return result


def _explain(problems: list[tuple[str, str]]) -> str:
    """把"到底哪一条写错了"说清楚，别只丢一句通用提示。"""
    if not problems:
        return "没有可解析的 SMILES（注意教材写法 CH3CH2OH 不是 SMILES）"
    detail = "；".join(f"{name}「{smi[:36]}」" for name, smi in problems[:3])
    more = f" 等 {len(problems)} 处" if len(problems) > 3 else ""
    return f"SMILES 无法解析：{detail}{more}。注意教材写法 CH3CH2OH 不是 SMILES，应写成 CCO"


@router.get("/health")
async def chem_health() -> dict[str, Any]:
    """设置页/排错用：本机是否装了 RDKit。"""
    return {"available": chem.available()}
