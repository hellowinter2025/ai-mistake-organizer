"""切题与预处理算法测试。"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app import imaging
from tests.conftest import make_page


@pytest.fixture(scope="module")
def page(page_image):
    return Image.open(page_image["path"]).convert("RGB")


def test_analyse_finds_all_questions(page):
    res = imaging.analyse(page)
    cands = res["candidates"]
    assert 4 <= len(cands) <= 6
    heights = [c["h"] for c in cands]
    assert max(heights) < res["height"] * 0.4
    # 题块自上而下排列
    ys = [c["y"] for c in cands]
    assert ys == sorted(ys)


def test_red_marks_attached_to_questions(page):
    res = imaging.analyse(page)
    marked = [c for c in res["candidates"] if c["mark_count"] > 0]
    assert marked, "应当检测到红笔批改痕迹"
    assert any("cross" in c["mark_kinds"] or "check" in c["mark_kinds"] for c in marked)
    assert all(c["wrong_score"] > 0 for c in marked)


def test_marks_are_red_not_black(page):
    """黑色印刷体不应被当成批改痕迹。"""
    import cv2

    bgr = imaging.pil_to_cv(page)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    mark = imaging.marking_mask(bgr)
    ink = imaging.ink_mask(gray)
    assert np.count_nonzero(mark) < np.count_nonzero(ink) * 0.25


def test_title_not_selected_but_split(page):
    res = imaging.analyse(page)
    first = res["candidates"][0]
    assert first["y"] < 140
    assert first["h"] < 60, "标题行应被单独切出"


def test_crop_pads_edges(page):
    box = [100, 150, 200, 80]
    out = imaging.crop(page, box)
    assert out.width >= 200 and out.height >= 80
    box2 = [0, 0, 200, 80]
    out2 = imaging.crop(page, box2)
    assert out2.width >= 200


def test_crop_rejects_tiny_box(page):
    with pytest.raises(ValueError):
        imaging.crop(page, [10, 10, 2, 2])


def test_merge_boxes():
    merged = imaging.merge_boxes([
        {"x": 10, "y": 20, "w": 30, "h": 40},
        {"x": 50, "y": 10, "w": 20, "h": 30},
    ])
    assert merged == {"x": 10, "y": 10, "w": 60, "h": 50}


def test_two_column_layout():
    """双栏版面应被识别成 2 栏，且题目不跨栏合并。"""
    img = Image.new("RGB", (1240, 1600), "white")
    d = ImageDraw.Draw(img)
    from tests.conftest import _font

    body = _font(24)
    for x0 in (60, 660):
        for i in range(3):
            y = 100 + i * 260
            for k in range(5):
                d.text((x0 + 6, y + k * 34), "已知函数求最小值并说明理由", font=body, fill=(15, 15, 15))
    res = imaging.analyse(img)
    assert res["columns"] == 2
    assert len(res["candidates"]) >= 6
    assert {c["column"] for c in res["candidates"]} == {0, 1}


def test_deskew_detects_rotation():
    img = Image.new("RGB", (1000, 1200), "white")
    d = ImageDraw.Draw(img)
    for i in range(12):
        d.text((160, 100 + i * 70), "数学题目示例文字数学题目示例文字", fill=(0, 0, 0))
    rotated = img.rotate(-4, resample=Image.BICUBIC, expand=False, fillcolor="white")
    gray = np.array(rotated.convert("L"))
    _out, angle = imaging.deskew(gray)
    assert abs(abs(angle) - 4) < 1.5, angle


def test_perspective_correction_optional(page):
    res = imaging.analyse(page, correct_perspective=False)
    assert res["perspective_corrected"] is False
    assert res["candidates"]


def test_analyse_is_json_serialisable(page):
    import json

    res = imaging.analyse(page)
    json.dumps(res, ensure_ascii=False)
    for c in res["candidates"]:
        assert isinstance(c["has_number"], bool)


def test_gap_ratio_effect(page):
    tight = imaging.analyse(page, gap_ratio=1.0)
    loose = imaging.analyse(page, gap_ratio=2.6)
    assert len(tight["candidates"]) >= len(loose["candidates"])
