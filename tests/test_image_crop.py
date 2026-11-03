"""选区裁剪与预览接口：给「不调模型」的录入链路兜底。

这一层的存在意义就是把「裁图」和「调模型」拆开：外部（人或 agent 自己读图）
只需要前者。所以这里除了功能正确，还要断言**确实没有触发 AI 调用**。
"""
from __future__ import annotations

import io

import pytest
from PIL import Image


def _png_bytes(page_image) -> bytes:
    buf = io.BytesIO()
    Image.open(page_image["path"]).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def uploaded(client, page_image):
    files = {"files": ("page.png", _png_bytes(page_image), "image/png")}
    res = client.post("/api/images", files=files)
    assert res.status_code == 200, res.text
    return res.json()["images"][0]


def test_crop_returns_usable_token(client, uploaded):
    res = client.post(f"/api/images/{uploaded['id']}/crop", json={"bbox": [90, 140, 700, 90]})
    assert res.status_code == 200, res.text
    crop = res.json()["crops"][0]
    assert len(crop["token"]) == 24
    # 裁剪会留一点白边，宽高比原框略大
    assert crop["width"] >= 700 and crop["height"] >= 90

    served = client.get(f"/api/crops/{crop['token']}")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/jpeg")
    with Image.open(io.BytesIO(served.content)) as im:
        assert im.size == (crop["width"], crop["height"])


def test_crop_accepts_normalized_box(client, uploaded):
    """0~1 的比例写法：整宽的中间 1/4 高度。"""
    res = client.post(f"/api/images/{uploaded['id']}/crop", json={"bbox": [0, 0.25, 1, 0.25]})
    assert res.status_code == 200, res.text
    crop = res.json()["crops"][0]
    assert abs(crop["width"] - uploaded["width"]) < 40
    assert abs(crop["height"] - uploaded["height"] * 0.25) < 40


def test_crop_multiple_boxes_keep_order(client, uploaded):
    boxes = [[90, 140, 700, 90], [90, 290, 700, 90], [90, 460, 700, 90]]
    res = client.post(f"/api/images/{uploaded['id']}/crop", json={"boxes": boxes})
    assert res.status_code == 200, res.text
    crops = res.json()["crops"]
    assert len(crops) == 3
    assert [c["bbox"] for c in crops] == boxes  # 顺序与传入一致，调用方才好对号
    # 三块内容不同 → token 必须不同
    assert len({c["token"] for c in crops}) == 3


def test_crop_rejects_tiny_box(client, uploaded):
    res = client.post(f"/api/images/{uploaded['id']}/crop", json={"bbox": [10, 10, 8, 6]})
    assert res.status_code == 400
    assert "太小" in res.json()["detail"]


def test_crop_rejects_too_many_boxes(client, uploaded):
    boxes = [[90, 140, 300, 60]] * 41
    res = client.post(f"/api/images/{uploaded['id']}/crop", json={"boxes": boxes})
    assert res.status_code == 400
    assert "最多" in res.json()["detail"]


def test_crop_requires_bbox(client, uploaded):
    res = client.post(f"/api/images/{uploaded['id']}/crop", json={})
    assert res.status_code == 400


def test_crop_never_calls_ai(client, uploaded, monkeypatch):
    """即使 AI 客户端整个炸掉，裁剪也必须照常工作。"""
    from app.ai import client as ai_client

    def boom(*args, **kwargs):
        raise AssertionError("裁剪链路不该调用模型")

    monkeypatch.setattr(ai_client, "solve_question", boom)
    res = client.post(f"/api/images/{uploaded['id']}/crop", json={"bbox": [90, 140, 700, 90]})
    assert res.status_code == 200, res.text


def test_preview_draws_boxes(client, uploaded):
    res = client.post(f"/api/images/{uploaded['id']}/preview", json={"boxes": [[90, 140, 700, 90]]})
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(res.content)) as im:
        assert im.format == "PNG"
        assert max(im.size) <= 1800


def test_preview_defaults_to_auto_candidates(client, uploaded):
    """不传框就跑自动切题：试卷页面至少要能给出一个候选。"""
    res = client.post(f"/api/images/{uploaded['id']}/preview", json={})
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "image/png"


def test_preview_with_grid_ruler(client, uploaded):
    """刻度尺是估 --span 的依据：没有它只能靠肉眼猜，实测会差 0.02 以上。"""
    plain = client.post(f"/api/images/{uploaded['id']}/preview", json={})
    gridded = client.post(f"/api/images/{uploaded['id']}/preview", json={"grid": True})
    assert gridded.status_code == 200
    with Image.open(io.BytesIO(gridded.content)) as im:
        assert im.format == "PNG"
    # 画了刻度线，体积必然变大（内容不同就不可能是同一张）
    assert gridded.content != plain.content


def test_bands_lists_blank_rows(client, uploaded):
    """空白带是定 --span 边界的依据：边界落在空白里就不会切掉半行字。"""
    res = client.post(f"/api/images/{uploaded['id']}/bands", json={"min_height": 6, "density": 0.01})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["height"] == uploaded["height"]
    assert data["bands"], "试卷页面至少应该有一条能用的空白带"
    for band in data["bands"]:
        assert 0 <= band["from"] < band["to"] <= data["height"]
        assert band["px"] == band["to"] - band["from"]
        assert band["px"] >= 6


def test_bands_uses_adaptive_threshold_not_raw_gray(client, uploaded):
    """纸底有阴影时，裸灰度阈值会把整张图判成墨迹（一条空白带都找不到）。"""
    res = client.post(f"/api/images/{uploaded['id']}/bands", json={"min_height": 4})
    assert res.status_code == 200
    assert len(res.json()["bands"]) >= 3


def test_crop_token_flows_into_question(client, uploaded):
    """裁出来的 token 能直接入库，且原题截图能取回（导出要用）。"""
    crop = client.post(f"/api/images/{uploaded['id']}/crop", json={"bbox": [90, 140, 700, 120]}).json()["crops"][0]
    res = client.post(
        "/api/questions",
        json={
            "stem": "已知函数 $f(x)=x^2-4x+3$，求它在 $[0,3]$ 上的最小值。",
            "answer": "-1",
            "subject": "数学",
            "question_type": "解答题",
            "crop_token": crop["token"],
            "image_id": uploaded["id"],
            "bbox": crop["bbox"],
        },
    )
    assert res.status_code == 200, res.text
    q = res.json()["question"]
    assert q["crop_url"] == f"/api/crops/{crop['token']}"
    assert client.get(q["crop_url"]).status_code == 200
    client.delete(f"/api/questions/{q['uid']}")
