"""安全层：主机校验、CSRF 防护、上传文件体检、安全响应头。

威胁模型（本机桌面应用）：
1. 恶意网页对本机 http://127.0.0.1:8765 发起跨站请求（CSRF / 表单提交）→
   Origin 校验 + 自定义头 X-App-Token + JSON Content-Type 三重拦截；
2. DNS rebinding 把外部域名解析到 127.0.0.1 → Host 白名单拦截；
3. 上传恶意图片（超大解压、构造的 polyglot 文件、带 EXIF 的 HTML/JS）→
   magic bytes 校验 + PIL 解码 + 重新编码，彻底剥离原始字节；
4. 路径穿越 → 全部资源用服务端生成的不透明 ID 访问，永不接受用户路径。
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
from typing import Iterable

from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import APP_TOKEN, MAX_IMAGE_PIXELS, MAX_REQUEST_BYTES, MAX_UPLOAD_BYTES

ALLOWED_HOSTS_FALLBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
ALLOWED_MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"RIFF": "webp",  # 需进一步校验 WEBP 标记
    b"BM": "bmp",
    b"GIF8": "gif",
}
# iPhone 默认拍 HEIC，是"拍照整理错题"最高频的真实输入
HEIF_BRANDS = {
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx",
    b"mif1", b"msf1", b"heif", b"avif", b"avis",
}
try:  # 可选依赖：装了就能直接吃 HEIC，没装则给出明确提示
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORT = True
except Exception:  # noqa: BLE001 - 缺依赖不应影响启动
    HEIF_SUPPORT = False
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    # 浏览器扩展的注入脚本会在页面里建 blob worker，没有这条会刷一片 CSP 报错；
    # 这里只放行 worker，不放行 script-src，页面内联脚本仍然禁止。
    "worker-src 'self' blob:; "
    "frame-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


class UploadError(ValueError):
    pass


def client_is_local(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback


class SecurityMiddleware(BaseHTTPMiddleware):
    """统一处理 Host/Origin/Token 校验与安全响应头。"""

    def __init__(self, app, allowed_hosts: Iterable[str] = ()) -> None:
        super().__init__(app)
        self.allowed_hosts = {h.lower() for h in allowed_hosts} or set(ALLOWED_HOSTS_FALLBACK)

    async def dispatch(self, request: Request, call_next):
        host_header = (request.headers.get("host") or "").strip().lower()
        hostname = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else host_header.split("]")[0] + "]"
        if hostname not in self.allowed_hosts:
            return JSONResponse({"error": "非法 Host"}, status_code=421)

        if request.method in UNSAFE_METHODS:
            length = (request.headers.get("content-length") or "").strip()
            if length.isdigit() and int(length) > MAX_REQUEST_BYTES:
                return JSONResponse(
                    {"error": f"上传内容过大（单次上限 {MAX_REQUEST_BYTES // 1024 // 1024}MB），请分批上传"},
                    status_code=413,
                )

        if request.method in UNSAFE_METHODS and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            if origin and not _same_origin(origin, host_header):
                return JSONResponse({"error": "跨站请求被拒绝", "code": "origin"}, status_code=403)
            if referer and not _same_origin(referer, host_header):
                return JSONResponse({"error": "跨站请求被拒绝", "code": "origin"}, status_code=403)
            if request.headers.get("x-app-token") != APP_TOKEN:
                # code=token 让前端自动重新拉取令牌并重试一次（服务重启后旧页面可自愈）
                return JSONResponse(
                    {"error": "访问令牌已失效，正在自动刷新，请重试", "code": "token"},
                    status_code=403,
                )

        response: Response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        path = request.url.path
        if path == "/" or path.endswith(".html"):
            response.headers["Content-Security-Policy"] = CSP
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif path == "/" or path.endswith((".html", ".js", ".css")):
            # 本地应用，代码一改就该立刻生效；图片/字体仍走 ETag 正常缓存
            response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _same_origin(url: str, host_header: str) -> bool:
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    return (parts.netloc or "").lower() == host_header


def sniff_kind(raw: bytes) -> str:
    if len(raw) >= 12 and raw[4:8] == b"ftyp" and raw[8:12].lower() in HEIF_BRANDS:
        if not HEIF_SUPPORT:
            raise UploadError(
                "这是 iPhone 的 HEIC 格式，当前环境未启用解析；"
                "可在手机「设置 → 相机 → 格式」改为「兼容性最佳」，或先把图片另存为 JPG"
            )
        return "heif"
    for magic, kind in ALLOWED_MAGIC.items():
        if raw.startswith(magic):
            if kind == "webp" and raw[8:12] != b"WEBP":
                continue
            return kind
    hint = "JPEG / PNG / WEBP / BMP / GIF" + (" / HEIC" if HEIF_SUPPORT else "")
    raise UploadError(f"不支持的图片格式（支持 {hint}）")


def load_upload(raw: bytes, declared_name: str = "") -> tuple[Image.Image, dict]:
    """校验并规范化上传图片，返回 (RGB 图像, 元信息)。"""
    if not raw:
        raise UploadError("文件是空的")
    if len(raw) > MAX_UPLOAD_BYTES:
        mb = MAX_UPLOAD_BYTES / 1024 / 1024
        got = len(raw) / 1024 / 1024
        raise UploadError(f"图片 {got:.1f}MB，超过单张 {mb:.0f}MB 上限")
    kind = sniff_kind(raw)
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
    except Image.DecompressionBombError as exc:
        raise UploadError("图片像素异常巨大，疑似损坏或恶意构造") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UploadError(f"图片已损坏或不完整：{exc}") from exc
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Image.DecompressionBombError as exc:
        raise UploadError("图片像素异常巨大，疑似损坏或恶意构造") from exc
    except Exception as exc:  # noqa: BLE001 - PIL 抛出的异常类型较杂
        raise UploadError(f"图片解码失败：{exc}") from exc
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise UploadError("图片像素过多，请先压缩后再上传")
    if img.width < 120 or img.height < 120:
        raise UploadError(f"图片过小（{img.width}×{img.height}），无法识别题目")
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    meta = {
        "kind": kind,
        "width": img.width,
        "height": img.height,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "orig_name": (declared_name or "")[:120],
    }
    return img, meta


def encode_for_ai(img: Image.Image, max_edge: int = 1600, quality: int = 88) -> bytes:
    """给视觉模型用的 JPEG：限制长边并压缩，避免请求体过大。"""
    work = img.copy()
    if max(work.size) > max_edge:
        work.thumbnail((max_edge, max_edge), Image.LANCZOS)
    if work.mode != "RGB":
        work = work.convert("RGB")
    buf = io.BytesIO()
    work.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
