"""题图分发：把服务端栅格化好的 PNG 发给前端。

前端**只拿 `<img>` 地址，零 SVG 处理** —— 预览、题库、导出用的都是
`svg_to_png` 这一份产物，所见即所得。以前预览在浏览器里用 DOMPurify 插
SVG，和导出走两条链，化学键被清洗掉都要等导出才发现。
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..config import CACHE_DIR

router = APIRouter(prefix="/api/figures", tags=["figures"])

# svg_to_png 的缓存文件名：fig_<sha1前20位>.png
_FIG_KEY = re.compile(r"^fig_[a-f0-9]{20}$")


@router.get("/img/{key}")
async def figure_image(key: str) -> FileResponse:
    key = key.strip()
    if key.endswith(".png"):
        key = key[:-4]
    if not _FIG_KEY.match(key):
        raise HTTPException(status_code=400, detail="非法图片标识")
    path = CACHE_DIR / f"{key}.png"
    if not path.exists() or path.stat().st_size <= 1200:
        raise HTTPException(status_code=404, detail="图片不存在或未渲染")
    return FileResponse(path, media_type="image/png")
