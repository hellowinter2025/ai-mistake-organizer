"""题图 SVG：安全过滤与栅格化。"""
from __future__ import annotations

import pytest

from app.ai import parser
from app.render.svg import sanitize_svg, svg_size, svg_to_png

TRIANGLE = (
    '<svg viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg">'
    '<polygon points="200,40 60,250 340,250" fill="none" stroke="#111" stroke-width="2"/>'
    '<text x="200" y="32" font-size="16" text-anchor="middle">A</text>'
    '<line x1="200" y1="40" x2="200" y2="250" stroke="#111" stroke-dasharray="4 3"/>'
    '</svg>'
)


def test_sanitize_keeps_drawing():
    out = sanitize_svg(TRIANGLE)
    assert out and "<polygon" in out and "<text" in out
    assert 'viewBox="0 0 400 300"' in out
    assert "stroke-dasharray" in out


def test_sanitize_strips_script():
    evil = TRIANGLE.replace("</svg>", '<script>alert(1)</script></svg>')
    out = sanitize_svg(evil)
    assert out and "script" not in out.lower()


def test_sanitize_strips_event_and_href():
    evil = (
        '<svg viewBox="0 0 100 100"><rect width="10" height="10" onload="alert(1)" '
        'href="javascript:alert(1)" style="fill:url(#x)"/></svg>'
    )
    out = sanitize_svg(evil)
    assert out
    assert "onload" not in out.lower()
    assert "javascript" not in out.lower()
    assert "style=" not in out


def test_sanitize_drops_foreign_object_and_image():
    evil = (
        '<svg viewBox="0 0 100 100"><foreignObject><body xmlns="http://www.w3.org/1999/xhtml">'
        '<script>alert(1)</script></body></foreignObject>'
        '<image href="http://evil/x.png" width="10" height="10"/>'
        '<circle cx="5" cy="5" r="4" fill="#000"/></svg>'
    )
    out = sanitize_svg(evil)
    assert out and "foreignObject" not in out
    assert "<image" not in out
    assert "<circle" in out


def test_sanitize_rejects_non_svg():
    assert sanitize_svg("hello") is None
    assert sanitize_svg("<svg></svg>") is None  # 没有任何绘图元素
    assert sanitize_svg("") is None


def test_sanitize_adds_viewbox():
    out = sanitize_svg('<svg width="200" height="120"><line x1="0" y1="0" x2="10" y2="10" stroke="#000"/></svg>')
    assert out and 'viewBox="0 0 200 120"' in out


def test_svg_size():
    assert svg_size(TRIANGLE) == (400, 300)
    assert svg_size('<svg><line x1="0" y1="0" x2="1" y2="1"/></svg>') == (400, 300)


def test_parser_keeps_valid_svg():
    import json

    raw = json.dumps(
        {
            "question_text": "如图，求面积",
            "answer": "12",
            "needs_figure": True,
            "figure_svg": TRIANGLE,
        },
        ensure_ascii=False,
    )
    res = parser.parse(raw)
    assert res.data["figure_svg"].startswith("<svg")
    assert res.data["needs_figure"] is True


def test_parser_flags_broken_svg():
    raw = '{"question_text": "如图", "answer": "1", "figure_svg": "<svg><script>x</script></svg>"}'
    res = parser.parse(raw)
    assert res.data["figure_svg"] == ""
    assert res.data["needs_figure"] is True
    assert any("SVG" in w for w in res.warnings)


@pytest.mark.integration
def test_svg_to_png_renders():
    png = svg_to_png(TRIANGLE, width_px=400, scale=1)
    if png is None:
        pytest.skip("未找到 Edge/Chrome，跳过栅格化测试")
    assert png.exists() and png.stat().st_size > 1200
    from PIL import Image

    with Image.open(png) as im:
        assert im.width >= 300 and im.height >= 200


# ---------------------------------------------------------------- style 白名单
# RDKit 把化学键的 stroke 写在 style 里，早先整条删掉 → 卷面上只剩 OH、O 几个字母。

def test_sanitize_keeps_safe_inline_style():
    svg = ('<svg viewBox="0 0 10 10"><path d="M0,0 L10,10" '
           'style="fill:none;stroke:#000000;stroke-width:2.0px"/>'
           '<text style="font-size:12px;fill:#000">A</text></svg>')
    out = sanitize_svg(svg)
    assert "stroke:#000000" in out and "stroke-width:2.0px" in out
    assert "font-size:12px" in out


def test_sanitize_strips_dangerous_style():
    svg = ('<svg viewBox="0 0 10 10">'
           '<rect style="fill:url(http://evil/x);background-image:url(javascript:1);'
           'width:expression(alert(1));stroke:#000"/></svg>')
    out = sanitize_svg(svg)
    assert "url(" not in out and "expression" not in out and "javascript" not in out
    # 同一条里合法的 stroke 仍然保留
    assert "stroke:#000" in out


def test_sanitize_drops_unknown_style_props():
    svg = '<svg viewBox="0 0 10 10"><rect style="position:fixed;behavior:url(x);fill:#123"/></svg>'
    out = sanitize_svg(svg)
    assert "position" not in out and "behavior" not in out
    assert "fill:#123" in out


# ---- 死图检测与复活：早期 sanitize 一刀切删 style，键线变成画不出来的裸 path ----

def test_is_dead_svg_detects_missing_stroke():
    from app.render.svg import is_dead_svg

    # 键线 path 被剥掉 style 后：stroke 默认 none、fill 填不了零面积线段 → 什么都画不出
    dead = '<svg viewBox="0 0 10 10"><path d="M 0,0 L 10,10"/></svg>'
    assert is_dead_svg(dead)

    alive = ('<svg viewBox="0 0 10 10">'
             '<path d="M 0,0 L 10,10" style="fill:none;stroke:#000"/></svg>')
    assert not is_dead_svg(alive)

    # 只有填充图形（原子标签）的图不算死，它本来就该有 fill
    assert not is_dead_svg('<svg viewBox="0 0 10 10"><text>A</text></svg>')
    assert not is_dead_svg('')


def test_revive_rebuilds_dead_chem_svg():
    """存量坏图用 figure_smiles 重画，键线必须回来。"""
    pytest.importorskip("rdkit")
    from app.render.svg import is_dead_svg, revive_figure_svg

    dead = '<svg viewBox="0 0 300 120"><path d="M 0,0 L 10,10"/><path d="M 5,5 L 15,15"/></svg>'
    fixed = revive_figure_svg({"figure_svg": dead, "figure_smiles": {"乙醇": "CCO"}})
    assert fixed != dead
    assert not is_dead_svg(fixed)
    assert "stroke" in fixed


def test_revive_prefers_source_fields_over_stale_svg():
    """源字段（smiles/flow）是事实来源：排版算法升级后旧 SVG 必须被重新编译。

    以前是「figure_svg 健康就原样用」，结果库里 L 形旧图一直显示，用户以为
    没重新渲染。有源字段就现编（有哈希缓存，不贵）。
    """
    pytest.importorskip("rdkit")
    from app.render.svg import revive_figure_svg

    stale = ('<svg viewBox="0 0 10 10">'
             '<path d="M 0,0 L 10,10" style="fill:none;stroke:#000"/></svg>')
    out = revive_figure_svg({"figure_svg": stale, "figure_smiles": {"乙醇": "CCO"}})
    assert out != stale
    assert "stroke" in out and len(out) > len(stale)


def test_revive_without_source_fields_keeps_stored_svg():
    from app.render.svg import revive_figure_svg

    good = ('<svg viewBox="0 0 10 10">'
            '<path d="M 0,0 L 10,10" style="fill:none;stroke:#000"/></svg>')
    assert revive_figure_svg({"figure_svg": good}) == good


def test_revive_falls_back_to_original_when_rebuild_fails():
    """重画不出来（SMILES 非法）时原样返回，不把图弄丢。"""
    from app.render.svg import revive_figure_svg

    dead = '<svg viewBox="0 0 10 10"><path d="M 0,0 L 10,10"/></svg>'
    out = revive_figure_svg({"figure_svg": dead, "figure_smiles": {"X": "不是SMILES"}})
    assert out == dead


def test_enhance_figure_removes_red_marks():
    """框选题图时自动增强：红笔痕迹去掉、输出灰度、纸底拉白。"""
    import numpy as np
    from PIL import Image, ImageDraw

    from app.imaging import enhance_figure

    im = Image.new("RGB", (240, 120), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([30, 40, 120, 50], fill=(20, 20, 20))     # 黑色线条（要保留）
    d.line([(140, 20), (200, 90)], fill=(220, 20, 20), width=4)  # 红笔（要去掉）
    d.ellipse([10, 80, 60, 110], outline=(220, 20, 20), width=3)

    out = enhance_figure(im, upscale=1)
    assert out.mode == "L"
    arr = np.array(out)
    # 红笔区域应被填成接近白纸
    assert arr[55, 170] > 200
    # 原来的黑色线条仍在（不能被一起抹掉）
    assert arr[45, 70] < 90


def test_enhance_figure_whitens_grey_paper():
    """整张发灰的纸要变纯白：旧版只做全局拉伸，纸底停在 220 灰。"""
    import numpy as np
    from PIL import Image, ImageDraw

    from app.imaging import enhance_figure

    im = Image.new("RGB", (400, 200), (198, 198, 200))  # 灰纸
    d = ImageDraw.Draw(im)
    d.line([(40, 100), (360, 100)], fill=(90, 90, 90), width=2)  # 印刷细线

    out = np.array(enhance_figure(im, upscale=1))
    assert out[20, 20] > 245, "纸底应被拉成纯白"
    assert out[100, 200] < 60, "线条应被压黑"


def test_enhance_figure_removes_black_handwriting():
    """黑笔圈注没有颜色可用，靠「笔画宽 + 填充率低」识别后去掉。"""
    import numpy as np
    from PIL import Image, ImageDraw

    from app.imaging import enhance_figure

    im = Image.new("RGB", (420, 260), "white")
    d = ImageDraw.Draw(im)
    d.ellipse([60, 70, 340, 240], outline=(15, 15, 15), width=7)  # 手写大圈
    d.rectangle([180, 120, 220, 200], outline=(15, 15, 15), width=2)  # 印刷细框（保留）

    out = np.array(enhance_figure(im, upscale=1))
    # 圈的左侧（远处没有印刷内容）应被擦成白纸
    assert out[155, 61] > 200, "手写圈应被去掉"
    # 印刷细框的左竖边仍在
    assert out[160, 180] < 90, "印刷线条不能被误删"


def test_enhance_figure_keeps_size():
    from PIL import Image

    from app.imaging import enhance_figure

    im = Image.new("RGB", (123, 45), "white")
    assert enhance_figure(im, upscale=1).size == (123, 45)
