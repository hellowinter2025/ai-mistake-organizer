"""提示词的多学科分支：数学/物理走 LaTeX，其余学科走纯文本。"""
from __future__ import annotations

from app.ai import prompts


def test_math_uses_latex_rules():
    text = prompts.system_prompt(True, "数学")
    assert "所有公式必须写成 LaTeX" in text
    assert "LaTeX 公式用 $...$ 包裹" in text
    assert "数学" in text


def test_physics_also_uses_latex():
    assert prompts.uses_latex("物理")
    assert "所有公式必须写成 LaTeX" in prompts.system_prompt(True, "物理")


def test_chinese_subject_switches_to_plain_text():
    """语文题强行要求 LaTeX，会把《静夜思》写成 $\\text{...}$，必须换一套规则。"""
    text = prompts.system_prompt(True, "语文")
    assert not prompts.uses_latex("语文")
    assert "本学科不使用 LaTeX" in text
    assert "所有公式必须写成 LaTeX" not in text
    assert "学科不使用 LaTeX" in text
    assert "语文" in text
    assert "公式按原样抄写，不用 LaTeX" in text


def test_plain_rules_keep_json_discipline():
    """没有 LaTeX 也要保证单行、不出现英文双引号，否则解析器照样崩。"""
    text = prompts.system_prompt(True, "历史")
    assert "每个字段的值必须是**单行**" in text
    assert "不要出现英文双引号" in text


def test_text_mode_prompt_also_subject_aware():
    # 英语已改走专门的错点抽取规则（见 test_english_*），纯文本分支用历史测
    text = prompts.system_prompt(False, "历史")
    assert "历史" in text
    assert "[[题目]]" in text
    assert "本学科不使用 LaTeX" in text
    assert "公式用 $...$ 或 $$...$$" not in text


def test_subject_defaults_and_whitespace():
    assert prompts.normalise_subject("") == "数学"
    assert prompts.normalise_subject("  ") == "数学"
    assert prompts.normalise_subject(" 物理\n") == "物理"
    # 自定义学科照单全收，不强制落在预设列表里
    assert prompts.normalise_subject("信息技术") == "信息技术"


def test_build_messages_carries_subject_in_user_prompt():
    msgs = prompts.build_messages("AAAA", json_mode=True, extra="无", subject="化学")
    system = msgs[0]["content"]
    user_text = msgs[1]["content"][0]["text"]
    assert "化学" in system
    assert "学科：化学" in user_text
    # 图片仍然以 data URL 传入
    assert msgs[1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_schema_hint_changes_with_subject():
    math_hint = prompts.schema_hint("数学")
    chinese_hint = prompts.schema_hint("语文")
    assert "$...$" in math_hint
    assert "$...$" not in chinese_hint


# ---------------------------------------------------------------------------
# 化学学科（2026-10-31）：\ce{} 排化学式，SMILES 画结构式，两者不能混
# ---------------------------------------------------------------------------

def test_chem_prompt_mentions_ce_and_smiles():
    from app.ai import prompts

    text = prompts.system_prompt(True, "化学")
    assert r"\ce{}" in text
    assert "smiles" in text
    # 必须明确禁止教材写法，否则模型会写 CH3CH2OH 导致解析失败
    assert "CH3CH2OH" in text
    assert "标准 SMILES" in text


def test_chem_schema_has_smiles_field():
    from app.ai import prompts

    assert '"smiles"' in prompts.schema_hint("化学")
    assert '"smiles"' not in prompts.schema_hint("数学")


def test_math_and_liberal_arts_prompts_unchanged():
    from app.ai import prompts

    math = prompts.system_prompt(True, "数学")
    assert r"\frac{}{}" in math and "smiles" not in math
    chinese = prompts.system_prompt(True, "语文")
    assert "不使用 LaTeX" in chinese


# ---- 英语：给的是一段内容（短文改错/作文），只输出「错误关键句 + 语法点」两栏 ----

def test_english_uses_own_rules():
    assert prompts.uses_english("英语")
    assert prompts.uses_english("英语（新高考）")
    assert prompts.uses_english("English")
    assert not prompts.uses_english("数学")
    assert not prompts.uses_english("语文")


def test_english_json_prompt_asks_for_blanked_sentences_and_grammar():
    text = prompts.system_prompt(True, "英语")
    # 场景：一段内容而不是一道题
    assert "一段英语内容" in text
    assert "短文改错" in text and "书面表达" in text
    # 三栏输出：挖空句 + 答案 + 语法点
    assert "挖空" in text
    assert "`_____`" in text
    assert "错误的关键句" not in text or "挖空的错题句" in text
    assert "关键语法点" in text
    # 标小序号且对应
    assert "(1)(2)(3)" in text
    assert "严格一一对应" in text
    # 禁止长篇解析与整段转录
    assert "不要长篇解析" in text
    assert "不要把这段话抄一遍" in text
    # answer 要有内容
    assert "solution_fix、solution_alt 一律留空字符串" in text
    assert "answer 要填正确答案" in text


def test_english_schema_is_three_column():
    hint = prompts.schema_hint("英语")
    assert '"question_text"' in hint and "挖空" in hint
    assert '"answer"' in hint and "正确答案" in hint
    assert '"error_analysis"' in hint and "关键语法点" in hint
    assert '"solution_fix": ""' in hint
    # 英语不带化学结构式/流程图字段
    assert "smiles" not in hint and "flow" not in hint


def test_english_text_mode_uses_section_markers():
    """分节标记保持不变，parser 的 parse_sections 才不用动。"""
    text = prompts.system_prompt(False, "英语")
    for mark in ("[[题目]]", "[[题型]]", "[[答案]]", "[[错误原因]]", "[[纠正解析]]", "[[知识点]]"):
        assert mark in text
    # 文本模式也必须挖空（以前写"原样照抄含错误"，与 ENG_RULES 的挖空要求打架）
    assert "挖空的错题句" in text
    assert "_____" in text
    assert "原样照抄（含错误" not in text


def test_english_does_not_pick_latex_or_chem_rules():
    text = prompts.system_prompt(True, "英语")
    assert "所有公式必须写成 LaTeX" not in text
    assert r"\ce{" not in text
    assert "smiles" not in text.lower().split("json 结构")[0]


# ---- 化学：合成路线必须走 flow（每步带 smiles），不能只给三个孤立结构式 ----

def test_chem_routes_with_arrows_must_use_flow():
    text = prompts.system_prompt(True, "化学")
    # 分流判据写明：有箭头相连 = flow，互不相连 = smiles
    assert "只要结构式之间有箭头相连，就不能只填 smiles" in text
    assert "只给互不相连的独立结构式" in text
    # 合成路线示例带 label（箭头上的条件）和每步 smiles
    assert '"name": "X", "smiles"' in text
    assert 'CH3COOH / 浓H2SO4' in text
    assert "只填 smiles 不填 flow 是错的" in text
    # 工作要求里也压了这条
    assert "就必须走 flow 字段" in text


def test_chem_text_mode_has_flow_section():
    text = prompts.system_prompt(False, "化学")
    assert "[[工艺流程图]]" in text
    assert "[[结构式SMILES]]" in text
    assert "有箭头相连" in text
    # 非化学学科不出现这两个节
    plain = prompts.system_prompt(False, "历史")
    assert "[[工艺流程图]]" not in plain


def test_choice_rules_present_for_every_written_subject():
    """单选/多选必须说清楚 —— AI 默认单选，多选经常只给一个字母。"""
    for subject in ("数学", "物理", "化学", "语文", "历史"):
        text = prompts.system_prompt(True, subject)
        assert "单选还是多选" in text, subject
        assert "不要默认单选" in text, subject
        # 答案要写全，并给正例
        assert "ACD" in text, subject
    # 英语是短文改错，不涉及选择题
    assert "单选还是多选" not in prompts.system_prompt(True, "英语")


def test_choice_rules_also_in_text_mode():
    assert "单选还是多选" in prompts.system_prompt(False, "物理")


def test_schema_mentions_single_and_multiple_choice():
    hint = prompts.schema_hint("数学")
    assert "选择题（单选）" in hint and "选择题（多选）" in hint
    assert "正确项全部写完" in hint


def test_choice_type_is_judged_by_model_not_passed_in():
    """单选/多选要模型自己判，不能让用户去提示。"""
    text = prompts.system_prompt(True, "数学")
    # 不能要求用户额外说明题型
    assert "请在补充信息" not in text
    assert "由你自己判断" in text
    assert "必须自己判" in text  # 明确"由你判"，不依赖用户输入
    # 判据是"正确项的个数"，不是卷面印象
    assert "正确项的个数" in text
    assert "两个及以上" in text


def test_choice_rule_drops_bogus_signal():
    """以前把「则（　　）」当多选信号是错的 —— 那是单选最常见的收尾写法。"""
    assert "则（　　）" not in prompts.system_prompt(True, "数学")


def test_sub_questions_must_all_be_answered():
    for json_mode in (True, False):
        text = prompts.system_prompt(json_mode, "数学")
        assert "小问" in text and "全部作答" in text


def test_text_mode_asks_for_tikz_not_handwritten_svg():
    """文本模式以前让模型手写 SVG，和 JSON 模式的 TikZ 不一致，画出来还常被清洗掉。"""
    text = prompts.system_prompt(False, "物理")
    assert "[[图形TikZ]]" in text
    assert "[[图形SVG]]" not in text
    assert "<svg viewBox" not in text


def test_difficulty_rubric_is_stated():
    assert "压轴" in prompts.system_prompt(True, "数学")
    assert "压轴" in prompts.system_prompt(False, "数学")


def test_prompt_teaches_real_tikz_library_facts():
    """提示词里的库说明必须和引擎实际能力一致，否则模型会绕开正确画法。"""
    text = prompts.system_prompt(True, "数学")
    assert "自动加载 calc" in text                 # calc 已由引擎兜住，不用模型自己声明
    assert "usetikzlibrary{库名}" in text          # 其他库怎么加载也要说清
    assert "fillbetween" in text                  # 这个确实不能用，要说清
    assert "$(A1)!0.35!(D)$" in text              # 给出可直接照抄的定比分点写法


def test_figure_policy_defaults_to_drawing():
    """默认要自己画；只有实拍图才贴截图，而且必须给 figure_box。"""
    for subject in ("数学", "物理", "化学"):
        text = prompts.system_prompt(True, subject)
        assert "默认就自己画" in text, subject
        assert "figure_box" in text, subject

    math = prompts.system_prompt(True, "数学")
    assert "空间四边形" in math                    # 立体几何属于必须画的一类
    chem = prompts.system_prompt(True, "化学")
    assert "电子式" in chem and "要画" in chem


# ---------- 高中方法解题提示 ----------

def test_method_hint_required_for_latex_subjects():
    """数学/物理的解析必须点明方法与触发点，学生才知道下次怎么下手。"""
    for subject in ("数学", "物理"):
        for json_mode in (True, False):
            text = prompts.system_prompt(json_mode, subject)
            assert "方法：<名称>" in text, (subject, json_mode)
            assert "触发点：" in text, (subject, json_mode)


def test_method_hint_absent_for_text_subjects():
    """化学/语文没有「高中数学方法」这套东西，别硬塞。"""
    for subject in ("化学", "语文", "英语"):
        for json_mode in (True, False):
            text = prompts.system_prompt(json_mode, subject)
            assert "触发点：" not in text, (subject, json_mode)


def test_method_hint_is_shape_only_not_a_technique_list():
    """方法提示只规定**结构**（方法 → 触发点 → 过程），不列具体技巧。

    两点都踩过：① 点名禁用洛必达/泰勒/中值定理，等于把这些词写进提示词，反而给
    超纲思路开了口子；② 列一堆"可用技巧"（参变分离、点差法、隐零点…）同样在替模型
    预设思路，容易把题往某个套路上带。提示词只留"按教材来 + 三段结构"这个约束。
    """
    forbidden = (
        # 点名禁用的超纲工具
        "洛必达", "泰勒", "中值定理", "拉格朗日乘数",
        # 具体技巧清单
        "隐零点", "端点效应", "参变分离", "点差法", "设而不求", "错位相减",
        "整体法", "微元法", "极化恒等式",
    )
    for json_mode in (True, False):
        for subject in ("数学", "物理"):
            text = prompts.system_prompt(json_mode, subject)
            assert "方法：<名称>" in text
            assert "触发点：" in text
            assert "高中教材" in text
            for word in forbidden:
                assert word not in text, f"{subject} 提示词里不该出现「{word}」"


def test_solution_fix_description_mentions_method():
    """字段说明里也要写清顺序，否则模型只看 schema 就漏了方法行。"""
    assert "方法：<名称>" in prompts.schema_hint("数学")
    assert "方法：<名称>" in prompts.schema_hint("物理")
    assert "方法：<名称>" not in prompts.schema_hint("化学")
