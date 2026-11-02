"""上传链路专项测试。

真实使用里最容易炸的就是这里：手机拍的 HEIC、微信转存的畸形 JPEG、
一次拖 20 张、服务重启后旧页面继续上传……所以逐个场景钉死。
"""
from __future__ import annotations

import io
import itertools
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
from PIL import Image

from app import db
from app.config import BASE_DIR, MAX_IMAGES_PER_UPLOAD, MAX_UPLOAD_BYTES, UPLOAD_DIR
from app.security import HEIF_SUPPORT, UploadError, load_upload


# ---------------------------------------------------------------- 构造素材

_NONCE = itertools.count(1)


def make_image(width=800, height=1100, mode="RGB", color="white", unique=True) -> Image.Image:
    """每次生成的内容都不同，避免测试之间被上传去重合并掉。"""
    img = Image.new(mode, (width, height), color)
    if mode in ("RGB", "RGBA"):
        img.paste((30, 30, 30), (40, 40, width - 40, height // 2))
        if unique:
            n = next(_NONCE)
            img.putpixel((width - 1, height - 1), (n % 251, (n * 7) % 251, (n * 13) % 251))
    return img


def encode(img: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def png_header_only(width: int, height: int) -> bytes:
    """只声明尺寸、没有像素数据的 PNG：用来触发解压炸弹防护。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def post(client, name: str, raw: bytes, mime: str = "image/png", field: str = "files"):
    return client.post("/api/images", files={field: (name, raw, mime)})


# ---------------------------------------------------------------- A. 格式覆盖

def test_upload_jpeg(client):
    raw = encode(make_image(), "JPEG", quality=90)
    res = post(client, "页1.jpg", raw, "image/jpeg")
    assert res.status_code == 200, res.text
    assert res.json()["images"][0]["width"] == 800


def test_upload_png_with_alpha(client):
    """带透明通道的截图很常见，转存后不能变成黑底。"""
    img = Image.new("RGBA", (700, 900), (255, 255, 255, 0))
    img.paste((20, 20, 20), (50, 50, 400, 500))
    res = post(client, "透明.png", encode(img, "PNG"))
    assert res.status_code == 200, res.text
    image_id = res.json()["images"][0]["id"]
    saved = Image.open(UPLOAD_DIR / f"{image_id}.jpg").convert("RGB")
    assert saved.getpixel((10, 10))[0] > 200, "透明区域应被压成白底而不是黑底"


def test_upload_webp(client):
    res = post(client, "a.webp", encode(make_image(), "WEBP"), "image/webp")
    assert res.status_code == 200, res.text


def test_upload_bmp(client):
    res = post(client, "a.bmp", encode(make_image(400, 600), "BMP"), "image/bmp")
    assert res.status_code == 200, res.text


def test_upload_gif(client):
    res = post(client, "a.gif", encode(make_image(400, 600).convert("P"), "GIF"), "image/gif")
    assert res.status_code == 200, res.text


def test_upload_grayscale(client):
    res = post(client, "gray.png", encode(Image.new("L", (700, 900), 250), "PNG"))
    assert res.status_code == 200, res.text


@pytest.mark.skipif(not HEIF_SUPPORT, reason="未安装 pillow-heif")
def test_upload_iphone_heic(client):
    """iPhone 默认拍照格式，之前会被"不支持的格式"直接挡掉。"""
    raw = encode(make_image(), "HEIF", quality=80)
    assert raw[4:8] == b"ftyp"
    res = post(client, "IMG_0042.HEIC", raw, "image/heic")
    assert res.status_code == 200, res.text
    assert res.json()["images"][0]["width"] == 800


def test_upload_exif_rotation_is_applied(client):
    """手机竖拍的 EXIF Orientation 必须生效，否则切题会横着切。"""
    img = make_image(1200, 800)  # 横向
    exif = img.getexif()
    exif[274] = 6  # 顺时针旋转 90°
    raw = encode(img, "JPEG", exif=exif.tobytes(), quality=88)
    res = post(client, "rotate.jpg", raw, "image/jpeg")
    assert res.status_code == 200, res.text
    data = res.json()["images"][0]
    assert (data["width"], data["height"]) == (800, 1200), "EXIF 方向没有被应用"


# ---------------------------------------------------------------- B. 拒绝路径

@pytest.mark.parametrize(
    "name,raw,mime",
    [
        ("note.txt", b"hello world", "text/plain"),
        ("fake.png", b"%PDF-1.7\n%not an image", "image/png"),
        ("icon.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>', "image/svg+xml"),
        ("empty.png", b"", "image/png"),
    ],
)
def test_upload_rejects_non_image(client, name, raw, mime):
    res = post(client, name, raw, mime)
    assert res.status_code == 400, res.text
    assert res.json()["detail"]


def test_upload_rejects_truncated_jpeg(client):
    raw = encode(make_image(), "JPEG")[:400]
    res = post(client, "half.jpg", raw, "image/jpeg")
    assert res.status_code == 400
    assert "损坏" in res.json()["detail"] or "解码" in res.json()["detail"]


def test_upload_rejects_tiny_image(client):
    res = post(client, "tiny.png", encode(Image.new("RGB", (60, 60), "white"), "PNG"))
    assert res.status_code == 400
    assert "过小" in res.json()["detail"]


def test_upload_rejects_decompression_bomb(client):
    res = post(client, "bomb.png", png_header_only(30000, 30000))
    assert res.status_code == 400
    assert "像素" in res.json()["detail"]


def test_upload_rejects_oversize(client):
    payload = b"\xff\xd8\xff\xe0" + b"\x00" * (MAX_UPLOAD_BYTES + 1024)
    res = post(client, "huge.jpg", payload, "image/jpeg")
    assert res.status_code == 400, res.text
    assert "MB" in res.json()["detail"]


def test_upload_partial_failure_keeps_good_ones(client):
    """一张坏图不该让同批的好图全部丢掉 —— 这是之前整批 400 的硬伤。"""
    good = encode(make_image(), "PNG")
    res = client.post(
        "/api/images",
        files=[
            ("files", ("ok.png", good, "image/png")),
            ("files", ("bad.png", b"not an image at all", "image/png")),
        ],
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert len(data["images"]) == 1
    assert data["images"][0]["name"] == "ok.png"
    assert len(data["errors"]) == 1
    assert data["errors"][0]["name"] == "bad.png"


def test_upload_batch_beyond_limit(client):
    good = encode(make_image(), "PNG")
    files = [("files", (f"p{i}.png", good + bytes([i]), "image/png")) for i in range(MAX_IMAGES_PER_UPLOAD + 3)]
    res = client.post("/api/images", files=files)
    assert res.status_code == 200, res.text
    data = res.json()
    assert len(data["images"]) == MAX_IMAGES_PER_UPLOAD
    overflow = [e for e in data["errors"] if "分批" in e["error"]]
    assert len(overflow) == 3


# ---------------------------------------------------------------- C. 安全

def test_upload_without_token_is_403_with_code(client, page_image):
    raw = encode(make_image(), "PNG")
    res = client.post("/api/images", files={"files": ("a.png", raw, "image/png")}, headers={"X-App-Token": "stale"})
    assert res.status_code == 403
    body = res.json()
    assert body["code"] == "token", "前端靠 code 判断是否该自动换令牌"
    assert "令牌" in body["error"]


def test_upload_cross_origin_is_403_with_origin_code(client):
    raw = encode(make_image(), "PNG")
    res = client.post(
        "/api/images",
        files={"files": ("a.png", raw, "image/png")},
        headers={"Origin": "http://evil.example"},
    )
    assert res.status_code == 403
    assert res.json()["code"] == "origin"


def test_upload_rejects_bad_host(client):
    res = post_host = client.post(
        "/api/images",
        files={"files": ("a.png", encode(make_image(), "PNG"), "image/png")},
        headers={"Host": "rebind.example"},
    )
    assert post_host.status_code == 421


def test_filename_never_used_as_path(client, tmp_path):
    """文件名只做展示，落盘路径必须是服务端生成的 32 位十六进制 ID。"""
    raw = encode(make_image(), "PNG")
    tricky = "../../../evil.png"
    res = post(client, tricky, raw)
    assert res.status_code == 200, res.text
    image_id = res.json()["images"][0]["id"]
    assert len(image_id) == 32 and image_id.isalnum()
    assert not (BASE_DIR / "evil.png").exists()
    assert not (UPLOAD_DIR.parent.parent / "evil.png").exists()
    assert (UPLOAD_DIR / f"{image_id}.jpg").exists()
    # 展示名原样保留（只截断长度）
    assert db.get_image(image_id)["orig_name"] == tricky


def test_filename_with_cjk_and_long_name(client):
    raw = encode(make_image(), "PNG")
    long_name = "第一章 集合与常用逻辑用语 错题扫描件 2026-10-29 晚自习.png"
    res = post(client, long_name, raw)
    assert res.status_code == 200, res.text
    image_id = res.json()["images"][0]["id"]
    stored = db.get_image(image_id)["orig_name"]
    assert stored.startswith("第一章")
    assert len(stored) <= 120


def test_content_length_guard():
    """超大请求体应在读盘前被挡掉（直接验中间件，不真发 70MB）。"""
    from starlette.requests import Request

    from app.config import MAX_REQUEST_BYTES

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/images",
        "headers": [
            (b"host", b"127.0.0.1:8765"),
            (b"content-length", str(MAX_REQUEST_BYTES + 1).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8765),
        "scheme": "http",
        "query_string": b"",
        "http_version": "1.1",
    }

    async def call_next(_request):  # pragma: no cover - 被拦下就不会调用
        raise AssertionError("超限请求不应进入路由")

    import asyncio

    from app.security import SecurityMiddleware

    mw = SecurityMiddleware(app=None)
    res = asyncio.run(mw.dispatch(Request(scope), call_next))
    assert res.status_code == 413


# ---------------------------------------------------------------- D. 幂等与产物

def test_duplicate_upload_reuses_image(client):
    raw = encode(make_image(), "PNG")
    first = post(client, "same.png", raw).json()["images"][0]
    second = post(client, "same-again.png", raw).json()["images"][0]
    assert second["id"] == first["id"]
    assert second["reused"] is True
    count = db.query_one("SELECT COUNT(*) AS n FROM images WHERE sha256=?", (db.get_image(first["id"])["sha256"],))
    assert count["n"] == 1, "同一张图不该在库里存两份"


def test_saved_file_is_normalised_jpeg(client):
    """原图字节必须被彻底丢弃：统一重编码成 JPEG，顺带剥掉 EXIF/脚本尾巴。"""
    raw = encode(make_image(), "PNG")
    image_id = post(client, "x.png", raw).json()["images"][0]["id"]
    saved = (UPLOAD_DIR / f"{image_id}.jpg").read_bytes()
    assert saved[:2] == b"\xff\xd8"
    assert raw not in saved


def test_thumb_and_file_endpoints(client):
    image_id = post(client, "t.png", encode(make_image(1600, 2000), "PNG")).json()["images"][0]["id"]
    assert client.get(f"/api/images/{image_id}/file").status_code == 200
    thumb = client.get(f"/api/images/{image_id}/thumb?w=300")
    assert thumb.status_code == 200
    assert Image.open(io.BytesIO(thumb.content)).width == 300
    # 越界参数被夹紧而不是报错
    assert client.get(f"/api/images/{image_id}/thumb?w=99999").status_code == 200


def test_thumb_rejects_path_traversal(client):
    assert client.get("/api/images/..%2F..%2Fconfig/thumb").status_code == 404


def test_reanalyse_after_upload(client):
    image_id = post(client, "r.png", encode(make_image(), "PNG")).json()["images"][0]["id"]
    res = client.post(f"/api/images/{image_id}/analyze", json={"options": {"gap_ratio": 2.2}})
    assert res.status_code == 200
    assert isinstance(res.json()["candidates"], list)


# ---------------------------------------------------------------- E. 纯函数层

def test_load_upload_reports_reason_not_traceback():
    with pytest.raises(UploadError) as exc:
        load_upload(b"\x89PNG\r\n\x1a\nbroken")
    assert "损坏" in str(exc.value)


def test_load_upload_accepts_exact_size_limit():
    """刚好等于上限应放行，只多 1 字节才拒。"""
    img_bytes = encode(make_image(300, 300), "JPEG")
    padded = img_bytes + b"\x00" * (MAX_UPLOAD_BYTES - len(img_bytes))
    assert len(padded) == MAX_UPLOAD_BYTES
    img, meta = load_upload(padded, "edge.jpg")
    assert img.width == 300


# ---------------------------------------------------------------- F. 令牌持久化

def test_token_persists_across_process_restart():
    """两次独立进程读到的令牌必须一致，否则用户重启服务后页面就废了。"""
    code = "import sys; sys.path.insert(0, r'%s'); from app.config import APP_TOKEN; print(APP_TOKEN)" % BASE_DIR
    env = os.environ.copy()
    env.pop("MM_APP_TOKEN", None)
    runs = [
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=90)
        for _ in range(2)
    ]
    for r in runs:
        assert r.returncode == 0, r.stderr[:300]
    assert runs[0].stdout.strip() == runs[1].stdout.strip()
    assert len(runs[0].stdout.strip()) >= 32


def test_token_file_is_private():
    from app.config import TOKEN_PATH

    assert TOKEN_PATH.exists()
    if os.name == "nt":
        return
    mode = TOKEN_PATH.stat().st_mode & 0o777
    assert mode & 0o077 == 0, f"令牌文件权限过宽：{oct(mode)}"
