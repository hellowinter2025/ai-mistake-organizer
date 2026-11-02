"""路由公共工具：文件落盘、ID 校验、序列化。"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Any

from PIL import Image

from .. import db
from ..config import CACHE_DIR, CROP_DIR, UPLOAD_DIR

SAFE_ID = re.compile(r"^[a-f0-9]{12,40}$")
CROP_TOKEN = re.compile(r"^[a-f0-9]{16,40}$")


def require_id(value: str, pattern: re.Pattern[str] = SAFE_ID) -> str:
    if not value or not pattern.match(value):
        raise ValueError("非法资源标识")
    return value


def save_image(img: Image.Image, image_id: str) -> Path:
    path = UPLOAD_DIR / f"{image_id}.jpg"
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(path, "JPEG", quality=93, optimize=True, progressive=True)
    return path


def image_path(image_id: str) -> Path:
    require_id(image_id)
    path = UPLOAD_DIR / f"{image_id}.jpg"
    if not path.exists():
        raise FileNotFoundError("图片不存在")
    return path


def load_image(image_id: str) -> Image.Image:
    with Image.open(image_path(image_id)) as im:
        im.load()
        return im.convert("RGB")


def new_crop_token(seed: str) -> str:
    return hashlib.sha1(f"{seed}:{db.new_uid()}".encode()).hexdigest()[:24]


def save_crop(img: Image.Image, token: str) -> Path:
    require_id(token, CROP_TOKEN)
    path = CROP_DIR / f"{token}.jpg"
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(path, "JPEG", quality=92, optimize=True)
    return path


def crop_path(token: str) -> Path:
    """按 token 找裁剪图。原图裁剪是 .jpg，增强过的题图是 .png（无损，线稿更利）。"""
    require_id(token, CROP_TOKEN)
    for ext in (".jpg", ".png"):
        path = CROP_DIR / f"{token}{ext}"
        if path.exists():
            return path
    raise FileNotFoundError("图片片段不存在")


def thumbnail(image_id: str, width: int = 480) -> Path:
    require_id(image_id)
    width = max(160, min(1200, int(width)))
    src = image_path(image_id)
    out = CACHE_DIR / f"thumb_{image_id}_{width}.jpg"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    with Image.open(src) as im:
        im = im.convert("RGB")
        ratio = width / im.width
        im = im.resize((width, max(1, int(im.height * ratio))), Image.LANCZOS)
        im.save(out, "JPEG", quality=82, optimize=True)
    return out


def crop_url(token: str) -> str:
    return f"/api/crops/{token}"


def image_url(image_id: str) -> str:
    return f"/api/images/{image_id}/file"


def question_payload(q: dict[str, Any]) -> dict[str, Any]:
    """给前端的题目结构（附带可直接用于 <img> 的地址）。

    题图给的是 ``figure_png_url``（服务端栅格化的 PNG，按需渲染）——前端
    只放 ``<img>``，不碰 SVG，预览和导出天然一致。
    """
    out = dict(q)
    if q.get("crop_path"):
        stem = Path(str(q["crop_path"])).stem
        out["crop_url"] = f"/api/crops/{stem}"
    else:
        out["crop_url"] = None
    if q.get("figure_crop"):
        stem = Path(str(q["figure_crop"])).stem
        out["figure_crop_url"] = f"/api/crops/{stem}"
    else:
        out["figure_crop_url"] = None
    if q.get("image_id"):
        out["image_url"] = image_url(q["image_id"])
    out["figure_png_url"] = (
        f"/api/questions/{q['uid']}/figure.png" if q.get("figure_svg") else None
    )
    return out


def jpeg_data_uri(path: Path, max_edge: int = 1400) -> str:
    import base64

    with Image.open(path) as im:
        im = im.convert("RGB")
        if max(im.size) > max_edge:
            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
