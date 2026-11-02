# -*- coding: utf-8 -*-
"""化学工艺流程图渲染器。

TikZ 那条路走不通（wasm TeX 没中文字库），所以流程由 app.render.flow 自己排 SVG。
这里盯三件事：形态归一、排版方向、化学式下标。
"""
from __future__ import annotations

import pytest

from app.render import flow


ALUMINA = {
    "steps": [
        {"name": "铝土矿", "note": "Al2O3、Fe2O3"},
        {"name": "碱浸", "label": "过量 NaOH 溶液", "note": "SiO2 → Na2SiO3"},
        {"name": "过滤"},
        {"name": "酸化", "label": "通入过量 CO2"},
        {"name": "灼烧", "label": "高温"},
    ],
    "side": [{"after": 3, "name": "滤渣"}],
}


# ------------------------------------------------------------------ 形态归一

def test_normalise_dict_and_json_text():
    spec = flow.normalise(ALUMINA)
    assert len(spec["steps"]) == 5
    assert spec["steps"][1]["label"] == "过量 NaOH 溶液"
    from_text = flow.normalise(__import__("json").dumps(ALUMINA, ensure_ascii=False))
    assert from_text["steps"] == spec["steps"]


def test_normalise_accepts_bare_list():
    spec = flow.normalise(["浸出", "过滤", "蒸发"])
    assert [s["name"] for s in spec["steps"]] == ["浸出", "过滤", "蒸发"]


def test_normalise_rejects_single_step():
    with pytest.raises(flow.FlowError):
        flow.normalise({"steps": [{"name": "只有一步"}]})


def test_normalise_rejects_plain_prose():
    # 描述性文字没有箭头，认不出流程，该拒就拒
    with pytest.raises(flow.FlowError):
        flow.normalise("先加碱浸出，再过滤，最后灼烧")


def test_normalise_accepts_arrow_chain():
    # 模型偶尔直接甩一行「A→B→C」，切一切就能用
    for text in ("铝土矿->碱浸->过滤", "铝土矿→碱浸→过滤", "铝土矿 ==> 碱浸 ==> 过滤"):
        spec = flow.normalise(text)
        assert [s["name"] for s in spec["steps"]] == ["铝土矿", "碱浸", "过滤"]


def test_normalise_accepts_alias_keys():
    """模型写字段名很随意，同义词都得认，否则一个 key 不同整张图就没了。"""
    spec = flow.normalise(
        {
            "流程": [
                {"操作": "铝土矿"},
                {"工序": "碱浸", "条件": "过量 NaOH"},
                {"名称": "过滤", "备注": "除去不溶物"},
            ],
            "支线": [{"at": 2, "name": "滤渣"}],
            "方向": "竖向",
            "方框": "是",
        }
    )
    assert [s["name"] for s in spec["steps"]] == ["铝土矿", "碱浸", "过滤"]
    assert spec["steps"][1]["label"] == "过量 NaOH"
    assert spec["steps"][2]["note"] == "除去不溶物"
    assert spec["side"][0]["after"] == 2
    assert spec["direction"] == "v"
    assert spec["box"] is True


def test_normalise_accepts_step_dict_and_structure_alias():
    spec = flow.normalise({"steps": {"1": "铝土矿", "2": "碱浸"}})
    assert [s["name"] for s in spec["steps"]] == ["铝土矿", "碱浸"]
    organic = flow.normalise({"stages": [{"title": "苯", "结构式": "c1ccccc1"}, {"title": "苯酚"}]})
    assert organic["steps"][0]["smiles"] == "c1ccccc1"


def test_direction_defaults_to_horizontal():
    # 试卷上的流程图基本都是横向一条线，长流程也要显式写 "v" 才竖排
    six = {"steps": [{"name": f"步骤{i}"} for i in range(6)]}
    assert flow.normalise(six)["direction"] == "h"
    nine = {"steps": [{"name": f"步骤{i}"} for i in range(9)]}
    assert flow.normalise(nine)["direction"] == "h"
    assert flow.normalise({**nine, "direction": "v"})["direction"] == "v"


def test_side_position_is_clamped():
    spec = flow.normalise({"steps": [{"name": "A"}, {"name": "B"}], "side": [{"after": 99, "name": "滤渣"}]})
    assert spec["side"][0]["after"] == 2


def test_chem_digits_become_subscripts():
    spec = flow.normalise({"steps": [{"name": "辉铜矿", "note": "Cu2S"}, {"name": "酸浸", "note": "Ca(OH)2"}]})
    assert spec["steps"][0]["note"] == "Cu₂S"
    assert spec["steps"][1]["note"] == "Ca(OH)₂"
    # 非化学场景别乱改：「步骤2」「10%」保持原样
    assert flow.normalise({"steps": [{"name": "步骤2"}, {"name": "浓度 10%"}]})["steps"][0]["name"] == "步骤2"


# ------------------------------------------------------------------ 渲染

def test_render_produces_svg_with_cjk_and_arrows():
    svg = flow.render(ALUMINA, use_cache=False)
    assert svg.startswith("<svg")
    assert "碱浸" in svg and "滤渣" in svg
    assert "marker-end" in svg            # 箭头
    assert "Al₂O₃" in svg                 # 下标
    # 默认不画框：教材里的流程就是「物质名 + 箭头」
    assert svg.count("<rect") == 0


def test_box_is_opt_in():
    boxed = flow.render({**ALUMINA, "box": True}, use_cache=False)
    assert boxed.count("<rect") == 6      # 5 个主步骤 + 1 条支线


# ------------------------------------------------------- 结构式节点（有机合成路线）

ORGANIC = {
    "steps": [
        {"name": "苯", "smiles": "c1ccccc1"},
        {"name": "溴苯", "smiles": "Brc1ccccc1", "label": "Br2 / FeBr3"},
        {"name": "苯酚", "smiles": "Oc1ccccc1", "label": "NaOH 后酸化"},
    ]
}


def test_smiles_step_is_drawn_into_flow():
    pytest.importorskip("rdkit")
    spec = flow.normalise(ORGANIC)
    assert [s["smiles"] for s in spec["steps"]] == ["c1ccccc1", "Brc1ccccc1", "Oc1ccccc1"]
    svg = flow.render(ORGANIC, use_cache=False)
    assert svg.count("<path") > 20        # 三个苯环的骨架线条
    assert "溴苯" in svg                   # 物质名照样写在下面
    assert "Br" in svg and "OH" in svg    # 取代基


def test_mixed_steps_allow_text_and_structure():
    """有机合成里既有结构式节点、也有「过滤」这种纯操作节点。"""
    pytest.importorskip("rdkit")
    spec = flow.normalise(
        {
            "steps": [
                {"name": "苯酚", "smiles": "Oc1ccccc1"},
                {"name": "过滤"},
                {"name": "苯酚钠", "smiles": "[Na+].[O-]c1ccccc1"},
            ]
        }
    )
    assert len(spec["steps"]) == 3
    svg = flow.render({"steps": spec["steps"]}, use_cache=False)
    assert "过滤" in svg and "苯酚钠" in svg


def test_bad_smiles_degrades_instead_of_failing():
    """教材写法 CH3CH2OH 不是 SMILES：只降级这一步，整张图照画，并把原因带回去。"""
    warns: list[str] = []
    spec = flow.normalise(
        {"steps": [{"name": "乙醇", "smiles": "CH3CH2OH"}, {"name": "乙醛", "smiles": "CC=O"}]},
        warns=warns,
    )
    assert spec["steps"][0]["smiles"] == ""
    assert warns and "CH3CH2OH" in warns[0] and "CCO" in warns[0]
    svg = flow.render({"steps": [{"name": "乙醇", "smiles": "CH3CH2OH"}, {"name": "乙醛"}]}, use_cache=False)
    assert "乙醇" in svg


def test_flow_endpoint_returns_smiles_warnings(client):
    pytest.importorskip("rdkit")
    res = client.post(
        "/api/flow/render",
        json={"flow": {"steps": [{"name": "乙醇", "smiles": "CH3CH2OH"}, {"name": "乙醛", "smiles": "CC=O"}]}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["warnings"] and "CCO" in body["warnings"][0]


def test_long_flow_wraps_into_rows():
    """真的很长才折行；折行均分、不留孤行。

    用户要求「过长就整行铺开、灵活判断」——所以 10 步以内的普通流程
    现在一律单行（不折成 L 形），只有 16 步这种超长流程才折。
    """
    import re

    # 名字写长一些把单行宽度顶到 1.5 倍栏宽以上，才触发折行
    steps = [{"name": f"铝土矿碱浸沉铝工序{i}"} for i in range(1, 13)]
    svg = flow.render({"steps": steps}, use_cache=False)
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    assert box
    w, h = float(box.group(1)), float(box.group(2))
    assert w <= flow.MAX_ROW_W * 1.55, f"折行后仍过宽：{w}"
    assert h > 80, "折行后画布应该变高"
    assert "<polyline" in svg          # 行与行之间的折线


def test_short_flow_stays_on_one_row():
    import re

    svg = flow.render({"steps": [{"name": "A"}, {"name": "B"}, {"name": "C"}]}, use_cache=False)
    assert "<polyline" not in svg
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    assert float(box.group(2)) < 80


def test_medium_flow_also_stays_single_row():
    """10 步以内的流程一律单行：折行会排成 L 形拐角，用户明确不要。"""
    import re

    svg = flow.render({"steps": [{"name": f"工序{i}"} for i in range(1, 11)]}, use_cache=False)
    assert "<polyline" not in svg, "10 步不该折行"
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    assert float(box.group(2)) < 80


def test_any_flow_is_wide_even_when_not_flat():
    """流程图一律整行：L 形/折行后宽高比掉到 1.3 也算宽图，不能缩在角落。"""
    from app.render.svg import figure_is_wide

    svg = flow.render({"steps": [{"name": "X"}, {"name": "Y"}, {"name": "Z"}]}, use_cache=False)
    assert figure_is_wide({"figure_flow": {"steps": [1, 2]}}, svg)
    # 没有 figure_flow 的方图照旧放角落
    assert not figure_is_wide({"figure_svg": '<svg viewBox="0 0 100 100"></svg>'})


def test_label_heat_becomes_triangle():
    """存量 label 里的明文「加热」也转 △（卷面箭头条件写法）。"""
    spec = flow.normalise({
        "steps": [
            {"name": "X", "smiles": "c1ccccc1"},
            {"name": "Y", "smiles": "Brc1ccccc1", "label": "CH₂(COOH)₂、吡啶、加热"},
            {"name": "加热回流"},
        ]
    })
    assert "△" in spec["steps"][1]["label"] and "加热" not in spec["steps"][1]["label"]
    # 工序名「加热回流」不误伤
    assert spec["steps"][2]["name"] == "加热回流"


def test_folded_flow_still_counts_as_wide_figure():
    """折行后宽高比掉到 2 左右，导出时仍要整行铺开，否则缩成一小块。"""
    from app.render.svg import figure_is_wide

    svg = flow.render({"steps": [{"name": f"工序{i}"} for i in range(1, 11)]}, use_cache=False)
    assert figure_is_wide({"figure_flow": {"steps": [1, 2]}, "figure_svg": svg})
    # 方图不受影响
    assert not figure_is_wide({"figure_svg": '<svg viewBox="0 0 100 100"></svg>'})


def test_render_vertical_layout():
    spec = {"direction": "v", "steps": [{"name": "A"}, {"name": "B"}, {"name": "C"}]}
    svg = flow.render(spec, use_cache=False)
    # 竖排时画布应该明显偏高
    import re

    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    assert box and float(box.group(2)) > float(box.group(1))


def test_render_is_cached():
    first = flow.render(ALUMINA)
    second = flow.render(ALUMINA)
    assert first == second


def test_render_survives_long_labels():
    spec = {
        "steps": [
            {"name": "矿石"},
            {"name": "焙烧", "label": "通入过量空气并控制温度在 800 摄氏度左右持续反应"},
            {"name": "浸出"},
        ]
    }
    svg = flow.render(spec, use_cache=False)
    assert "<svg" in svg  # 超长标注要折行，不能把画布撑爆


# ------------------------------------------------------------------ 接口

def test_flow_endpoint(client):
    res = client.post("/api/flow/render", json={"flow": ALUMINA})
    assert res.status_code == 200, res.text
    body = res.json()
    assert "<svg" in body["svg"] and "碱浸" in body["svg"]
    assert len(body["spec"]["steps"]) == 5


def test_flow_endpoint_reports_bad_spec(client):
    res = client.post("/api/flow/render", json={"flow": {"steps": [{"name": "一步"}]}})
    assert res.status_code == 400
    assert "两个步骤" in res.json()["detail"]


def test_question_save_compiles_flow(client):
    res = client.post(
        "/api/questions",
        json={"stem": "流程如图", "subject": "化学", "figure_flow": ALUMINA, "needs_figure": False},
    )
    assert res.status_code == 200, res.text
    q = res.json()["question"]
    assert q["figure_svg"] and "碱浸" in q["figure_svg"]
    assert q["needs_figure"] is False
    assert len(q["figure_flow"]["steps"]) == 5
    client.delete(f"/api/questions/{q['uid']}")


def test_latex_in_labels_is_stripped_to_plain_text():
    r"""label 是画进 SVG 的纯文字：模型写的 $\ce{...}$/$\Delta$ 会原样印成反斜杠串。"""
    spec = flow.normalise({
        "steps": [
            {"name": "X", "smiles": "c1ccccc1"},
            {
                "name": "Y",
                "smiles": "Brc1ccccc1",
                "label": r"$\ce{CH2(COOH)2}$、吡啶、$\Delta$",
                "note": r"生成 $\ce{H2O}$",
            },
        ]
    })
    y = spec["steps"][1]
    assert "\\" not in y["label"] and "$" not in y["label"]
    # LaTeX 剥掉后照旧下标化：CH2(COOH)2 → CH₂(COOH)₂，\Delta → △（卷面样式）
    assert "CH₂(COOH)₂" in y["label"] and "△" in y["label"]
    assert "H₂O" in y["note"] and "\\ce" not in y["note"]
