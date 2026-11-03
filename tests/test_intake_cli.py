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
