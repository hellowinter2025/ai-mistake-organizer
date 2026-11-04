"""录入 skill 的脚本逻辑：字段校验、查重探针、选区换算。

这些是「录入质量」的地基——校验漏了脏数据会进库，查重探针错了会重复入库，
选区换算错了会裁偏。全部做成纯函数测试，不依赖服务。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# 优先测仓库内那份（随仓库分发的主副本），本机安装的那份作为兜底
MM_PATH = ROOT / "skills" / "错题录入" / "scripts" / "mm.py"
if not MM_PATH.is_file():
    MM_PATH = Path.home() / ".workbuddy" / "skills" / "错题录入" / "scripts" / "mm.py"

pytestmark = pytest.mark.skipif(not MM_PATH.is_file(), reason="skill 错题录入 未安装")


@pytest.fixture(scope="module")
def mm():
    spec = importlib.util.spec_from_file_location("mm_skill", MM_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _item(**over):
    base = {
        "subject": "数学",
        "question_type": "解答题",
        "stem": "已知函数 $f(x)=x^2-4x+3$，求它在区间 $[0,3]$ 上的最小值。",
        "answer": "-1",
        "error_reason": "配方后没有核对区间端点",
        "solution_fix": "方法：配方法\n触发点：给定闭区间求最值\n过程：……",
        "knowledge_points": ["二次函数的最值"],
        "difficulty": 2,
    }
    base.update(over)
    return base


def test_valid_item_passes(mm):
    errors, warns = mm.validate(_item())
    assert errors == []
    assert warns == []


def test_empty_stem_rejected(mm):
    errors, _ = mm.validate(_item(stem="   "))
    assert any("stem" in e for e in errors)


def test_unknown_subject_rejected(mm):
    errors, _ = mm.validate(_item(subject="高数"))
    assert any("subject" in e for e in errors)


def test_bad_question_type_rejected(mm):
    errors, _ = mm.validate(_item(question_type="单选"))
    assert any("question_type" in e for e in errors)


def test_literal_newline_rejected(mm):
    """`\\nA. ` 这种字面量要拦下来（页面不认，还会带崩公式）。"""
    errors, _ = mm.validate(_item(stem="下列正确的是\\nA. 甲\\nB. 乙"))
    assert any("字面量" in e for e in errors)


def test_tex_command_not_mistaken_for_literal(mm):
    """`\\ne` / `\\to` / `\\theta` 是 TeX 命令，不能当成字面量换行。"""
    errors, _ = mm.validate(_item(stem="由 $a\\ne b$ 且 $x\\to 0$、$\\theta=\\angle A$ 得"))
    assert not any("字面量" in e for e in errors)


def test_difficulty_out_of_range(mm):
    errors, _ = mm.validate(_item(difficulty=7))
    assert any("difficulty" in e for e in errors)


def test_single_choice_with_two_letters_warns(mm):
    _, warns = mm.validate(_item(question_type="选择题（单选）", answer="AC"))
    assert any("单选" in w for w in warns)


def test_choice_answer_must_be_letters(mm):
    _, warns = mm.validate(_item(question_type="选择题（多选）", answer="A、C"))
    assert any("纯字母" in w for w in warns)


def test_method_line_expected_for_math(mm):
    _, warns = mm.validate(_item(solution_fix="配方得最小值为 -1"))
    assert any("方法：" in w for w in warns)


def test_method_line_not_required_for_chemistry(mm):
    _, warns = mm.validate(_item(subject="化学", solution_fix="由电荷守恒得 x=2", solution_alt=""))
    assert not any("方法：" in w for w in warns)


def test_missing_answer_warns(mm):
    _, warns = mm.validate(_item(answer=""))
    assert any("answer" in w for w in warns)


def test_normalise_item_accepts_wrapped_fields(mm):
    wrapped = {"fields": _item(), "crop_token": "a" * 24, "bbox": [1, 2, 3, 4], "image_id": "b" * 32}
    item = mm.normalise_item(wrapped)
    assert item["crop_token"] == "a" * 24
    assert item["image_id"] == "b" * 32
    assert item["stem"] == _item()["stem"]


def test_normalise_item_splits_list_fields(mm):
    item = mm.normalise_item(_item(knowledge_points="二次函数、配方法; 最值", tags="易错，计算"))
    assert item["knowledge_points"] == ["二次函数", "配方法", "最值"]
    assert item["tags"] == ["易错", "计算"]


def test_normalise_title_ignores_punctuation_and_spacing(mm):
    a = mm.normalise_title("已知函数 $f(x)=x^2$，求最小值。")
    b = mm.normalise_title("已知函数$f(x)=x^2$,求最小值")
    assert a == b


def test_search_probe_picks_contiguous_chinese(mm):
    """探针必须是库中连着出现的片段：不能拿「去空白后」的文本去 LIKE。"""
    probe = mm.search_probe("已知函数 $f(x)=x^2-4x+3$，求它在区间 $[0,3]$ 上的最小值。")
    assert probe and " " not in probe
    assert probe in "已知函数 $f(x)=x^2-4x+3$，求它在区间 $[0,3]$ 上的最小值。"


def test_search_probe_falls_back_for_formula_only(mm):
    """整句没有连续中文时退回压缩文本，至少还能搜。"""
    probe = mm.search_probe("$\\frac{1}{2}x^2+3x-1$")
    assert probe


def test_parse_box_spec_requires_four_numbers(mm):
    assert mm.parse_box_spec("90,140,700,90") == [90.0, 140.0, 700.0, 90.0]


def test_parse_box_spec_rejects_bad_input(mm):
    with pytest.raises(SystemExit):
        mm.parse_box_spec("90,140,700")


def test_span_box_covers_full_width(mm):
    assert mm.span_box("0.1:0.4", {}) == [0.0, 0.1, 1.0, pytest.approx(0.3)]


def test_span_box_orders_reversed_input(mm):
    assert mm.span_box("0.4:0.1", {})[1] == 0.1


def test_combine_spans_builds_grid(mm):
    spans = [[0.0, 0.1, 1.0, 0.3], [0.0, 0.5, 1.0, 0.2]]
    xrs = [[0.0, 0.0, 0.5, 1.0], [0.5, 0.0, 0.5, 1.0]]
    boxes = mm.combine_spans(spans, xrs)
    assert len(boxes) == 4  # 两道题 × 两栏
    assert boxes[0] == [0.0, 0.1, 0.5, 0.3]
    assert boxes[1] == [0.5, 0.1, 0.5, 0.3]


def test_combine_spans_without_x_spans_full_width(mm):
    boxes = mm.combine_spans([[0.0, 0.2, 1.0, 0.3]], [])
    assert boxes == [[0.0, 0.2, 1.0, 0.3]]


def test_legacy_choice_type_only_warns(mm):
    """早期数据里题型只写「选择题」，不能直接判成非法（多选单选全靠猜）。"""
    errors, warns = mm.validate(_item(question_type="选择题", answer="B"))
    assert not any("question_type" in e for e in errors)
    assert any("旧写法" in w for w in warns)


# ---------- 图形：重绘的 TikZ 必须能提交、必须过静态检查 ----------

def test_normalise_item_keeps_tikz(mm):
    """回归：FIELDS 里漏了 figure_tikz 时，脚本会把重绘的图整段丢掉。"""
    item = mm.normalise_item(
        {"fields": _item(), "figure_tikz": "\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}"}
    )
    assert "figure_tikz" in item and "tikzpicture" in item["figure_tikz"]


def test_tikz_missing_environment_is_error(mm):
    errors, _ = mm.check_tikz("\\draw (0,0)--(1,1);")
    assert any("begin{tikzpicture}" in e for e in errors)


def test_tikz_unfinished_end_only_warns(mm):
    """截断的 \\end 由引擎补，不该拦入库。"""
    errors, warns = mm.check_tikz("\\begin{tikzpicture}\\draw (0,0)--(1,1);")
    assert not errors
    assert any("end{tikzpicture}" in w for w in warns)


def test_tikz_rejects_engine_breaking_commands(mm):
    for bad in ("\\usepackage{amsmath}", "\\documentclass{article}",
                "\\usepgfplotslibrary{fillbetween}"):
        errors, _ = mm.check_tikz("\\begin{tikzpicture}\\draw (0,0)--(1,1);" + bad + "\\end{tikzpicture}")
        assert errors, bad


def test_tikz_chinese_only_warns(mm):
    errors, warns = mm.check_tikz("\\begin{tikzpicture}\\node at (0,0) {中点};\\draw (0,0)--(1,1);\\end{tikzpicture}")
    assert not errors
    assert any("汉字" in w for w in warns)


def test_needs_figure_without_box_warns(mm):
    """贴截图却不给 figure_box，导出会把整段题干一起印上去。"""
    _, warns = mm.validate({**_item(), "needs_figure": 1})
    assert any("figure_box" in w for w in warns)


def test_tikz_with_needs_figure_warns(mm):
    _, warns = mm.validate(
        {**_item(), "needs_figure": 1,
         "figure_tikz": "\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}"}
    )
    assert any("needs_figure" in w for w in warns)


def test_normalize_figure_box_accepts_ratio_and_pixels(mm):
    assert mm.normalize_figure_box("0.1,0.2,0.3,0.4", 1000, 500) == [0.1, 0.2, 0.3, 0.4]
    assert mm.normalize_figure_box("100,100,400,200", 1000, 500) == [0.1, 0.2, 0.4, 0.4]


def test_normalize_figure_box_clamps_overflow(mm):
    # 宽高超出右/下边界时要收回来，否则裁图会失败
    box = mm.normalize_figure_box("0.8,0.8,0.9,0.9", 100, 100)
    assert box[0] + box[2] <= 1.0001
    assert box[1] + box[3] <= 1.0001


# ---------- 渲染自检：能不能发现「编译成功但画空了」 ----------

def _png(ink: bool) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (300, 200), (255, 255, 255))
    if ink:
        ImageDraw.Draw(img).line([(20, 100), (280, 100)], fill=(0, 0, 0), width=3)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_render_report_flags_blank_image(mm):
    warns, info = mm.render_report(_png(False), '<svg><text>x</text><path d="M0 0"/></svg>')
    assert any("空白" in w for w in warns)
    assert info["ink"] < 0.002


def test_render_report_passes_normal_image(mm):
    warns, info = mm.render_report(_png(True), '<svg><text>x</text><path d="M0 0"/></svg>')
    assert not warns, warns
    assert info["texts"] == 1


def test_render_report_flags_missing_labels(mm):
    warns, _ = mm.render_report(_png(True), '<svg><path d="M0 0"/></svg>')
    assert any("标注" in w for w in warns)


# ---------- 体检：按学科口径挑毛病 ----------

def _q(**over):
    base = {
        "subject": "数学",
        "stem": "已知函数 $f(x)=x^2-4x+3$，求最小值。",
        "answer": "-1",
        "knowledge_points": ["二次函数"],
        "solution_fix": "方法：配方法\n触发点：闭区间求最值\n过程：……",
        "solution_alt": "",
        "needs_figure": 0,
    }
    base.update(over)
    return base


def test_audit_math_accepts_method_lines(mm):
    assert mm.audit_question(_q()) == []


def test_audit_math_flags_missing_method_line(mm):
    issues = mm.audit_question(_q(solution_fix="配方得 $f(x)=(x-2)^2-1$，故最小值为 -1。"))
    assert any("方法" in i for i in issues)
    assert any("触发点" in i for i in issues)


def test_audit_chem_flags_letter_only_answer(mm):
    """化学只写一个字母等于没写解析。"""
    issues = mm.audit_question(
        _q(subject="化学", answer="B", solution_fix="", knowledge_points=["官能团"])
    )
    assert any("解析为空" in i for i in issues)


def test_audit_chem_flags_missing_equations(mm):
    issues = mm.audit_question(
        _q(subject="化学", answer="B",
           solution_fix="A 项正确。B 项错误，因为醇羟基不与氢氧化钠反应。")
    )
    assert any("化学式" in i or "方程式" in i for i in issues)


def test_audit_chem_flags_math_style_format(mm):
    issues = mm.audit_question(
        _q(subject="化学", answer="B",
           solution_fix="方法：逐项判断\nA 正确……$\\ce{NaOH}$ 反应")
    )
    assert any("不该用" in i for i in issues)


def test_audit_english_flags_whole_passage(mm):
    """十空全抄进来的是练习题，不是错题本。"""
    stem = "\n".join(f"({i}) They _____ here." for i in range(1, 11))
    issues = mm.audit_question(_q(subject="英语", stem=stem, answer="(1) are", solution_fix=""))
    assert any("整段" in i for i in issues)
    assert any("翻译" in i for i in issues)


def test_audit_flags_figure_without_box(mm):
    issues = mm.audit_question(_q(needs_figure=1, figure_box=None))
    assert any("figure_box" in i for i in issues)
