r"""化学结构式渲染与 \ce{} 降级。

RDKit 是可选依赖：没装的环境跳过渲染用例，只测「禁用时是否正确降级」。
"""
from __future__ import annotations

import pytest

from app.render import chem
from app.render.latex import ce_to_latex, ce_to_unicode, has_ce

needs_rdkit = pytest.mark.skipif(not chem.available(), reason="未安装 RDKit")


# ---------------------------------------------------------------- SMILES 清洗

def test_normalise_accepts_dict_list_and_string():
    assert chem.normalise({"苯酚": "Oc1ccccc1"}) == {"苯酚": "Oc1ccccc1"}
    assert list(chem.normalise(["CCO"]).values()) == ["CCO"]
    assert list(chem.normalise("CCO").values()) == ["CCO"]


@needs_rdkit
def test_normalise_drops_textbook_notation():
    """教材写法 CH3CH2OH 不是合法 SMILES，必须被剔掉而不是送进渲染。"""
    got = chem.normalise({"乙醇": "CH3CH2OH", "苯": "c1ccccc1"})
    assert got == {"苯": "c1ccccc1"}


@needs_rdkit
def test_normalise_drops_broken_smiles():
    assert chem.normalise({"坏": "CCO(", "好": "CCO"}) == {"好": "CCO"}


@needs_rdkit
def test_normalise_strips_spaces_and_backticks():
    assert chem.normalise({"苯": " `c1ccccc1` "}) == {"苯": "c1ccccc1"}


@needs_rdkit
def test_formula_from_smiles():
    assert chem.formula("Oc1ccccc1") == "C6H6O"
    assert chem.formula("CCOC(C)=O") == "C4H8O2"
    assert chem.formula("CH3CH2OH") == ""


# ---------------------------------------------------------------- 渲染

@needs_rdkit
def test_render_svg_is_single_canvas_with_all_molecules():
    svg = chem.render_svg({"苯酚": "Oc1ccccc1", "乙酸乙酯": "CCOC(C)=O"}, use_cache=False)
    assert svg.startswith("<svg")
    assert svg.count("<g transform=") == 2  # 两个分子并排
    assert "<rect" not in svg  # 白底矩形会挡住并排拼接


@needs_rdkit
def test_render_svg_single_molecule_has_no_translate_gap():
    svg = chem.render_svg({"乙醇": "CCO"}, use_cache=False)
    assert "translate(0,0)" in svg


@needs_rdkit
def test_render_png_is_png():
    data = chem.render_png({"乙醇": "CCO"}, width_px=600, use_cache=False)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


@needs_rdkit
def test_render_empty_raises():
    with pytest.raises(chem.ChemError):
        chem.render_svg({}, use_cache=False)


def test_disabled_without_rdkit(monkeypatch):
    """没装 RDKit 时要干净地降级，而不是抛 ImportError。"""
    monkeypatch.setattr(chem, "_HAS_RDKIT", False)
    assert chem.available() is False
    with pytest.raises(chem.ChemError):
        chem.render_svg({"乙醇": "CCO"}, use_cache=False)


# ---------------------------------------------------------------- \ce{} 降级

@pytest.mark.parametrize(
    "src,want",
    [
        (r"\ce{H2SO4}", r"\mathrm{H_{2}SO_{4}}"),
        (r"\ce{CH3COOCH2CH3}", r"\mathrm{CH_{3}COOCH_{2}CH_{3}}"),
        (r"\ce{Ca(OH)2}", r"\mathrm{Ca(OH)_{2}}"),
    ],
)
def test_ce_to_latex_subscripts(src, want):
    assert ce_to_latex(src) == want


def test_ce_to_latex_arrow_and_precipitate():
    got = ce_to_latex(r"\ce{Na2CO3 + CaCl2 -> CaCO3 v + 2NaCl}")
    assert r"\rightarrow" in got
    assert r"\downarrow" in got
    assert "v +" not in got  # 沉淀符号不能被当字母留下


def test_ce_to_latex_leaves_conditions_alone():
    """带反应条件的不硬转，交给上层退成纯文本。"""
    src = r"\ce{2H2O2 ->[MnO2] 2H2O + O2 ^}"
    assert has_ce(ce_to_latex(src))


def test_ce_to_unicode_fallback():
    assert ce_to_unicode(r"\ce{H2SO4}") == "H₂SO₄"
    assert ce_to_unicode(r"\ce{Ca^{2+}}") == "Ca²⁺"
    assert "↓" in ce_to_unicode(r"\ce{CaCO3 v}")


def test_ce_mixed_with_normal_latex():
    got = ce_to_latex(r"n(\ce{H2O}) = \frac{m}{18}")
    assert got == r"n(\mathrm{H_{2}O}) = \frac{m}{18}"


# ------------------------------------------------- JSON 文本形态（2026-10-31）
# figure_smiles 在库里是 TEXT，编辑器会把它原样回传；早先这里把整段 JSON 当成
# 一个 SMILES，于是三条正确的结构式全被丢掉，还报「没有可解析的 SMILES」。

JSON_TEXT = '{"X": "OCc1ccc(C=O)cc1", "Y": "OCc1ccc(C=CC(=O)O)cc1", "Z": "OCc1ccc(C=CC(=O)OC)cc1"}'


@needs_rdkit
def test_normalise_accepts_json_string():
    got = chem.normalise(JSON_TEXT)
    assert got == {
        "X": "OCc1ccc(C=O)cc1",
        "Y": "OCc1ccc(C=CC(=O)O)cc1",
        "Z": "OCc1ccc(C=CC(=O)OC)cc1",
    }


@needs_rdkit
def test_normalise_reports_what_it_dropped():
    rejects: list[tuple[str, str]] = []
    got = chem.normalise({"乙醇": "CH3CH2OH", "苯": "c1ccccc1"}, rejects=rejects)
    assert got == {"苯": "c1ccccc1"}
    assert rejects == [("乙醇", "CH3CH2OH")]


@needs_rdkit
def test_chem_endpoint_accepts_json_string(client):
    res = client.post("/api/chem/render", json={"smiles": JSON_TEXT})
    assert res.status_code == 200, res.text
    data = res.json()
    assert set(data["molecules"]) == {"X", "Y", "Z"}
    assert "<svg" in data["svg"]


@needs_rdkit
def test_chem_endpoint_partial_failure_has_warning(client):
    res = client.post("/api/chem/render", json={"smiles": {"乙醇": "CCO", "坏": "CCO("}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert list(body["molecules"]) == ["乙醇"]
    assert body["warnings"] and "坏" in body["warnings"][0]


@needs_rdkit
def test_chem_endpoint_explains_bad_smiles(client):
    res = client.post("/api/chem/render", json={"smiles": {"乙醇": "CH3CH2OH"}})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "乙醇" in detail and "CH3CH2OH" in detail and "CCO" in detail
