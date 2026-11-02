"""测试公共配置：隔离运行数据目录，避免污染真实题库。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(tempfile.mkdtemp(prefix="mm_test_"))
os.environ.setdefault("MM_DATA_DIR", str(TMP_DIR))
os.environ.setdefault("MM_LOG_DIR", str(TMP_DIR / "logs"))
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_page(path: Path, *, title: str = "高 一 数 学 测 试") -> dict:
    """生成一张模拟试卷：标题 + 4 题 + 红笔批改痕迹，返回关键坐标。"""
    width, height = 1240, 1754
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    d.text((width // 2 - 110, 70), title, font=_font(34), fill="black")
    lines = [
        (150, "1. 已知函数 f(x)=x^2-4x+3，求它在区间 [0,3] 上的最小值。"),
        (176, "   解：f(x)=(x-2)^2-1，所以最小值为 -1。"),
        (300, "2. 解方程 2x^2-5x+2=0。"),
        (326, "   解：(2x-1)(x-2)=0，所以 x=1/2 或 x=2。"),
        (470, "3. 在三角形 ABC 中，AB=AC=5，BC=6，求它的面积。"),
        (496, "   解：作 AD 垂直 BC 于 D，BD=3，AD=4，面积 S=12。"),
        (670, "4. 计算积分。"),
        (696, "   解：原式 = 2。"),
    ]
    for y, text in lines:
        d.text((90, y), text, font=_font(26), fill="black")
    # 红叉（第 1 题）、红勾（第 2 题）、红色划线（第 3 题）
    d.line([(1000, 150), (1070, 215)], fill=(210, 40, 35), width=7)
    d.line([(1070, 150), (1000, 215)], fill=(210, 40, 35), width=7)
    d.line([(1010, 330), (1025, 352)], fill=(210, 40, 35), width=7)
    d.line([(1025, 352), (1070, 300)], fill=(210, 40, 35), width=7)
    d.line([(1000, 600), (1120, 600)], fill=(210, 40, 35), width=6)
    img.save(path)
    return {"path": path, "width": width, "height": height}


@pytest.fixture(scope="session")
def page_image(tmp_path_factory) -> dict:
    path = tmp_path_factory.mktemp("img") / "page.png"
    return make_page(path)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.config import APP_TOKEN
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.headers.update({"X-App-Token": APP_TOKEN})
        yield c
