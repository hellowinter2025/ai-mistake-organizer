"""化学工艺流程图 / 有机合成路线：结构化描述 → SVG。

为什么不用 TikZ 画：node-tikzjax 那套 wasm TeX 没装中文字库，图里一出现「过滤」
「滤渣」就直接崩（``Package inputenc Error: Unicode character 矿``）。而工艺流程图
恰恰离不开中文，所以这里自己排 SVG —— 浏览器 / 打印用的 Chrome / Word 栅格化
都能正常用系统中文字体。

输入是一份很窄的结构化描述（让模型填，而不是让它算坐标）：

    {
      "steps": [
        {"name": "铝土矿", "note": "Al2O3、SiO2"},
        {"name": "碱浸", "label": "过量 NaOH", "note": "SiO2 → Na2SiO3"},
        {"name": "过滤"}
      ],
      "side": [{"after": 2, "name": "滤渣"}]
    }

- ``steps`` 按顺序排成一条链，``label`` 写在**进入这一步的箭头**上
- ``side`` 表示从某个步骤分出去的支线（滤渣、母液、循环回用等）
- ``direction`` 默认 ``h``（试卷上的流程图基本都是横向一条），显式写 ``v`` 才竖排
- ``box`` 默认 **false**：教材里的流程是「物质名 + 箭头」，不套方框。
  个别题目确实要求方框流程时再写 ``"box": true``

某一步如果是**具体的有机物**，可以再给 ``smiles``：直接画结构式骨架，物质名写在
下面。有机合成路线（苯 → 溴苯 → 苯酚）就是这么表达的：

    {"steps": [
        {"name": "苯", "smiles": "c1ccccc1"},
        {"name": "溴苯", "smiles": "Brc1ccccc1", "label": "Br2 / FeBr3"},
        {"name": "苯酚", "smiles": "Oc1ccccc1", "label": "NaOH 后酸化"}
    ]}

坐标、间距、箭头位置全由这里算，模型只负责"流程是什么"。
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.sax.saxutils as sx
from typing import Any

from ..config import CACHE_DIR

CACHE = CACHE_DIR / "flow"

FONT = "'Microsoft YaHei','PingFang SC','Noto Sans CJK SC',SimHei,sans-serif"
BOX_H = 34.0          # 带框时框高
BOX_PAD = 12.0        # 带框时框内左右留白
BOX_MIN_W = 58.0
TEXT_PAD = 7.0        # 无框时文字两侧留给箭头的空隙（单侧）
GAP_H = 46.0          # 横向箭头长度
GAP_V = 42.0          # 纵向箭头长度
# 字号层级对齐卷面：结构式明显大于箭头上的条件字（条件是小字两行）
ARROW_LABEL_SIZE = 8.5
NAME_SIZE = 12.0
NOTE_SIZE = 9.0
SIDE_GAP = 26.0
MARGIN = 6.0
UNDER_GAP = 12.0      # 带框时框底到下方文字的距离
# 一行的最大宽度：**单行优先**，折行的 L 形很难看（用户明确不要）。
# 导出时整行铺开缩到 16.2cm（459pt），900pt 宽缩完还有 0.51 倍、11pt 条件字
# 印出来约 5.6pt —— 长流程字号小一点可以接受，比折成两行强。
# 只有真的很长（单行超过 1000pt 或 12 步以上）才折成**均分**的多行。
MAX_ROW_W = 1000.0
ROW_GAP = 36.0        # 折行时两行之间的通道（画折线 + 写进入条件）

# 内嵌结构式：按独立渲染的标准画布（340×240、线宽 2）画好再等比缩进节点，
# 视觉效果与单独渲染的 SMILES 图完全一致（细线、比例协调）。
# 直接在 80×54 的小画布上画会把键线画粗、双键糊在一起，很难看。
FIG_W = 124.0
FIG_H = FIG_W * 240.0 / 340.0   # = 87.5，保持与标准画布同宽高比
FIG_PAD = 5.0
FIG_NAME_SIZE = 10.0  # 结构式节点下方那个物质名（X/Y/Z，比条件字略大）

MAX_STEPS = 12

# 渲染排版版本：影响输出的常量（尺寸/字号/画法）有变动就 +1。
# 缓存键 = 流程数据哈希 + 本版本号 —— 否则改了排版，同一份流程数据
# 一直命中旧缓存文件，图看着像"没重新渲染"。
LAYOUT_VER = "2"


class FlowError(RuntimeError):
    """流程图描述不合法，或渲染不出来。"""


def _text_width(text: str, size: float) -> float:
    """粗略估宽：汉字按 1em，ASCII 按 0.56em。够排版用，不需要真实字体度量。"""
    width = 0.0
    for ch in text or "":
        width += size if ord(ch) > 0x2E7F else size * 0.56
    return width


def _wrap(text: str, size: float, limit: float, max_lines: int = 3) -> list[str]:
    """按视觉宽度折行：优先在空格/顿号处断，实在没有就按字断。

    早期按字符数对半砍，会把 "过量 NaOH 溶液" 切成 "过量 Na" + "OH 溶液"，
    化学式被拦腰截断，看着像错字。
    """
    text = (text or "").strip()
    if not text:
        return []
    if _text_width(text, size) <= limit:
        return [text]
    lines: list[str] = []
    cur = ""
    for token in re.split(r"(\s+|(?<=[、，,；;]))", text):
        if not token:
            continue
        if _text_width(cur + token, size) <= limit or not cur:
            if _text_width(cur + token, size) <= limit:
                cur += token
                continue
            # 单个 token 就超宽：按字符硬断
            buf = ""
            for ch in token:
                if _text_width(cur + buf + ch, size) > limit and (cur + buf):
                    lines.append((cur + buf).strip())
                    cur, buf = "", ""
                buf += ch
            cur += buf
            continue
        lines.append(cur.strip())
        cur = token.lstrip() if token.strip() else ""
    if cur.strip():
        lines.append(cur.strip())
    if len(lines) > max_lines:  # 太长的标注截断，别把版面撑爆
        lines = lines[: max_lines - 1] + [lines[-1][: len(lines[-1])]]
        lines = [ln for ln in lines][:max_lines]
    return [ln for ln in lines if ln]


_SUB_DIGITS = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
_CHEM_SUB = re.compile(r"(?<=[A-Za-z\)])(\d+)")


def _plain_chem(text: str) -> str:
    """把 label 里的 LaTeX 化学式还原成纯文字。

    流程图是 SVG 里画出来的纯文字，模型顺手写的 `$\\ce{CH2(COOH)2}$`、`\\Delta`
    会原样印在图上（一串反斜杠），必须先剥成「CH2(COOH)2」「加热」再下标化。
    """
    if not text:
        return text
    t = text
    t = re.sub(r"\$?\\ce\{([^{}]*)\}\$?", lambda m: m.group(1), t)   # $\ce{X}$ → X
    t = re.sub(r"\$([^$]*)\$", lambda m: m.group(1), t)              # $X$ → X
    t = (
        t.replace("\\Delta", "△")
        .replace("\\delta", "Δ")
        .replace("\\times", "×")
        .replace("\\rightarrow", "→")
        .replace("\\longrightarrow", "→")
        .replace("\\to", "→")
    )
    t = t.replace("\\", "")
    return t.strip()


_LABEL_HEAT = re.compile(r"(?<![\u4e00-\u9fffA-Za-z0-9])加热(?![\u4e00-\u9fffA-Za-z0-9])")


def _label_plain(text: str) -> str:
    """箭头条件专用清洗：剥 LaTeX 之外，把独立的「加热」写成 △。

    卷面上加热条件就是箭头边一个三角；存量数据里 label 已经存成明文
    「…、吡啶、加热」，_plain_chem 认不出来，这里补一刀。整词匹配，
    「加热回流」这类不误伤。name/note 不走这里（工序名「加热」保持文字）。
    """
    return _LABEL_HEAT.sub("△", _plain_chem(text))


def _chem(text: str) -> str:
    """把化学式里的数字下标化：Al2O3 → Al₂O₃，Ca(OH)2 → Ca(OH)₂。

    流程图里全是化学式，直接写阿拉伯数字在成品上看着很业余；只在"字母或右括号
    后面"转换，所以「步骤2」「10%」这类不会被误伤。模型已经写了下标的话原样保留。
    """
    if not text:
        return text
    return _CHEM_SUB.sub(lambda m: m.group(1).translate(_SUB_DIGITS), text)


# 模型写字段名很随意：同一个"箭头上加什么试剂"，可能叫 label/arrow/reagent/条件/试剂。
# 这里按同义词逐个捞，拿到第一个非空值就用 —— 与其因为一个键名不同就整张图不画，
# 不如尽量认出来。
_STEP_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("name", "title", "text", "step", "操作", "名称", "工序", "物质", "物质名"),
    "label": (
        "label", "arrow", "arrow_label", "reagent", "condition", "cond",
        "条件", "试剂", "加入", "箭头上", "反应条件",
    ),
    "note": (
        "note", "notes", "detail", "desc", "description", "remark",
        "说明", "备注", "注释", "补充",
    ),
    "smiles": ("smiles", "smile", "smi", "structure", "mol", "结构式", "结构", "分子"),
}
_STEP_LIST_KEYS = ("steps", "step", "stages", "process", "flow_steps", "流程", "步骤", "工序", "阶段")
_SIDE_KEYS = ("side", "sides", "branch", "branches", "out", "支线", "副产物", "分支", "副线")
_VERTICAL_WORDS = {"v", "vertical", "column", "竖", "竖向", "竖排", "垂直", "纵向"}
_HORIZONTAL_WORDS = {"h", "horizontal", "row", "横", "横向", "横排", "水平"}

# 「铝土矿→碱浸→过滤」这种一行到底的写法
_ARROW_CHAIN = re.compile(r"(?:-{1,2}>|={1,}>|→|⟶|➔|⇒|—>)")


def _pick_any(item: Any, keys: tuple[str, ...], default: Any = "") -> Any:
    """按同义词顺序取第一个有值的字段。"""
    if not isinstance(item, dict):
        return default
    for key in keys:
        if key not in item:
            continue
        value = item[key]
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value.strip()
            continue
        if isinstance(value, (list, dict)):
            if value:
                return value
            continue
        return str(value).strip()
    return default


def _split_chain(text: str) -> list[str]:
    """把「A→B→C」切成步骤名。"""
    parts = [p.strip(" \t、,，;；") for p in _ARROW_CHAIN.split(text.strip())]
    return [p for p in parts if p]


def normalise(raw: Any, warns: list[str] | None = None) -> dict[str, Any]:
    """把模型给的各种形态收成标准结构。

    接受 dict / JSON 文本 / 列表（列表就当成 steps）。任何一步既没 name 又没
    ``smiles`` 就丢掉，别把半截流程画出来 —— 有机合成路线里节点就是结构式，
    未必有中文名。

    ``smiles`` 解析不了 / 本机没装 RDKit 时只把该步降级成纯文字，不连累整张图；
    降级原因通过 ``warns`` 回传，免得用户以为结构式画上了。
    """
    data: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise FlowError(f"流程图不是合法 JSON：{exc.msg}") from exc
        elif _ARROW_CHAIN.search(text):
            # 模型偶尔直接甩一行「铝土矿→碱浸→过滤」，比报错好
            data = {"steps": _split_chain(text)}
        else:
            raise FlowError("流程图需要 JSON 描述（steps/side）")
    if isinstance(data, list):
        data = {"steps": data}
    if not isinstance(data, dict):
        return {}

    from . import chem  # 局部导入：没装 RDKit 也不该影响纯文字流程图

    raw_steps = _pick_any(data, _STEP_LIST_KEYS)
    if isinstance(raw_steps, str):
        raw_steps = _split_chain(raw_steps)
    elif isinstance(raw_steps, dict):
        # {"1": "铝土矿", "2": "碱浸"} 这种把序号当键的写法
        raw_steps = [{_STEP_KEYS["name"][0]: v} for v in raw_steps.values()]

    steps: list[dict[str, str]] = []
    for item in raw_steps or []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _pick_any(item, _STEP_KEYS["name"])
        smi = _pick_any(item, _STEP_KEYS["smiles"]).strip("`'\" ")
        if not name and not smi:
            continue
        if smi:
            smi = smi.replace(" ", "")
            if not chem.available():
                if warns is not None:
                    warns.append(f"「{name or smi[:20]}」的结构式没画：本机未安装 RDKit")
                smi = ""
            elif not chem.is_valid(smi):
                if warns is not None:
                    warns.append(
                        f"「{name or '结构式'}」的 SMILES「{smi[:24]}」无法解析"
                        "（教材写法 CH3CH2OH 不是 SMILES，应写 CCO）"
                    )
                smi = ""
        steps.append(
            {
                # 先剥 LaTeX 再截断：$\ce{CH2(COOH)2}$ 要变「CH2(COOH)2」，
                # 直接截会把 \ce{ 从中切断，图上留一串反斜杠
                "name": _chem(_plain_chem(name)[:24]),
                "label": _chem(_label_plain(_pick_any(item, _STEP_KEYS["label"]))[:40]),
                "note": _chem(_plain_chem(_pick_any(item, _STEP_KEYS["note"]))[:40]),
                "smiles": smi,
            }
        )
    if len(steps) < 2:
        raise FlowError("流程图至少要两个步骤")

    raw_sides = _pick_any(data, _SIDE_KEYS, default=[])
    sides: list[dict[str, Any]] = []
    for item in raw_sides if isinstance(raw_sides, (list, tuple)) else []:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _pick_any(item, _STEP_KEYS["name"])
        if not name:
            continue
        try:
            after = int(_pick_any(item, ("after", "at", "step", "序号", "第几步")) or len(steps))
        except (TypeError, ValueError):
            after = len(steps)
        sides.append(
            {
                "after": max(1, min(after, len(steps))),
                "name": _chem(_plain_chem(name)[:20]),
                "label": _chem(_label_plain(_pick_any(item, _STEP_KEYS["label"]))[:24]),
            }
        )

    # 默认横向：试卷上的工艺流程图 / 合成路线基本都是横向一条；竖排是少数
    # 长流程题才用，得显式写 "direction": "v"。
    direction = _pick_any(data, ("direction", "orient", "layout", "方向", "排列")).lower()
    if direction in _VERTICAL_WORDS:
        direction = "v"
    elif direction in _HORIZONTAL_WORDS:
        direction = "h"
    else:
        direction = "h"
    boxed = _pick_any(data, ("box", "boxed", "frame", "border", "方框", "边框")).lower() in (
        "1", "true", "yes", "on", "是", "真",
    ) or data.get("box") is True
    return {
        "steps": steps[:MAX_STEPS],
        "side": sides[:6],
        "direction": direction,
        "box": boxed,
    }


# ------------------------------------------------------------------ 节点尺寸

def _step_w(step: dict[str, Any], boxed: bool) -> float:
    if step.get("smiles"):
        base = FIG_W + (FIG_PAD * 2 if boxed else 0)
        if boxed:
            # 带框时名字在框里居中，框得容得下
            return max(base, _text_width(step["name"], NAME_SIZE) + BOX_PAD * 2, BOX_MIN_W)
        # 无框时结构式直接顶到箭头，留点空，别让取代基（-Br）贴着箭头
        return base + TEXT_PAD * 2
    text_w = _text_width(step["name"], NAME_SIZE)
    return text_w + (BOX_PAD * 2 if boxed else TEXT_PAD * 2)


def _step_h(step: dict[str, Any], boxed: bool) -> float:
    if step.get("smiles"):
        return FIG_H + (FIG_PAD * 2 if boxed else 0)
    return BOX_H if boxed else NAME_SIZE + 10


# ------------------------------------------------------------------ 节点绘制

def _node(x: float, y: float, w: float, h: float, name: str, smiles: str, boxed: bool) -> str:
    """一个流程节点。带结构式时画骨架，文字（若有）留给下面单独画。"""
    rect = ""
    if boxed:
        rect = (
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="3" '
            f'fill="none" stroke="#000" stroke-width="0.9"/>'
        )
    if smiles:
        from . import chem

        try:
            # 与独立结构式渲染完全同款（340×240 画布、线宽 2、单色），再等比缩放
            tile = chem.draw_tile(smiles)
        except Exception:  # noqa: BLE001 - 画不出来就当纯文字节点，别整张图崩掉
            tile = ""
        if tile:
            scale = FIG_W / 340.0
            tx = x + (w - FIG_W) / 2
            ty = y + (h - FIG_H) / 2
            return (
                rect
                + f'<g transform="translate({tx:.1f},{ty:.1f}) scale({scale:.4f})">{tile}</g>'
            )
    if not name:
        return rect
    size = NAME_SIZE if not smiles else FIG_NAME_SIZE
    return (
        rect
        + f'<text x="{x + w / 2:.1f}" y="{y + h / 2:.1f}" font-family="{FONT}" '
        f'font-size="{size}" fill="#000" text-anchor="middle" '
        f'dominant-baseline="central">{sx.escape(name)}</text>'
    )


def _under_lines(step: dict[str, Any]) -> list[tuple[str, float, str]]:
    """节点下方要写的文字：(内容, 字号, 颜色)。"""
    out: list[tuple[str, float, str]] = []
    if step.get("smiles") and step.get("name"):
        out.append((step["name"], FIG_NAME_SIZE, "#000"))
    if step.get("note"):
        out.append((step["note"], NOTE_SIZE, "#444"))
    return out


def _under_line_height(size: float) -> float:
    return size + 6


def _under_width(step: dict[str, Any]) -> float:
    return max((_text_width(t, size) for t, size, _ in _under_lines(step)), default=0.0)


def _under_height(step: dict[str, Any]) -> float:
    lines = _under_lines(step)
    if not lines:
        return 0.0
    # 末尾 +8 是最后一行字的下沿：_under 里基线画在 cur+size*0.82，
    # 只按 size+6 累加会算短，最后一行被画布边缘切掉
    return sum(_under_line_height(size) for _, size, _ in lines) + 8


def _under(x: float, y: float, step: dict[str, Any], gap: float, anchor: str = "middle") -> str:
    """从节点底 y 开始往下写下方文字，返回 svg 片段。"""
    parts: list[str] = []
    cur = y + gap
    for text, size, color in _under_lines(step):
        parts.append(
            f'<text x="{x:.1f}" y="{cur + size * 0.82:.1f}" font-family="{FONT}" '
            f'font-size="{size}" fill="{color}" text-anchor="{anchor}">{sx.escape(text)}</text>'
        )
        cur += _under_line_height(size)
    return "".join(parts)


def _arrow(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="#000" stroke-width="0.9" marker-end="url(#flow-arrow)"/>'
    )


def _label(x: float, y: float, text: str, anchor: str = "middle") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" '
        f'font-size="{ARROW_LABEL_SIZE}" fill="#111" text-anchor="{anchor}">'
        f"{sx.escape(text)}</text>"
    )


def _elbow(x1: float, y1: float, x2: float, y2: float, mid_y: float) -> str:
    """折行用的拐弯箭头：下 → 横 → 下，最后一段带箭头。"""
    if abs(x1 - x2) < 0.5:
        return _arrow(x1, y1, x2, y2)
    return (
        f'<polyline points="{x1:.1f},{y1:.1f} {x1:.1f},{mid_y:.1f} '
        f'{x2:.1f},{mid_y:.1f} {x2:.1f},{y2:.1f}" fill="none" stroke="#000" '
        f'stroke-width="0.9" marker-end="url(#flow-arrow)"/>'
    )


def _plan_rows(widths: list[float], gaps: list[float], max_w: float) -> list[list[int]]:
    """把节点按顺序排成一行或几行。**单行优先**，折行是最后手段。

    流程图折行后会变 L 形拐角（用户明确不要），所以只要一行放得下就绝不折：
    整行铺开缩到 16.2cm 后字号小一点，也比拐个弯好看。只有真的很长
    （单行超过 max_w 且 6 步以上）才折成**步数均分**的多行 —— 均分比贪心
    装箱好看，后者会排成 7+2 这种一边倒。

    ``gaps[i]`` 是第 i-1 与第 i 步之间的箭头长度（行首那一步不占间距）。
    """

    def row_w(idxs: list[int]) -> float:
        return sum(widths[i] for i in idxs) + sum(gaps[i] for i in idxs[1:])

    total = row_w(list(range(len(widths))))
    n = len(widths)
    # 单行优先（用户：过长就整行铺开，别折成 L 形）：只要没宽到离谱
    # （1.5 倍栏宽）就一行排完，导出时整行缩放；真放不下才折。
    if total <= max_w * 1.5:
        return [list(range(n))]
    # 真的很长：按步数均分行数，每行连续取一段
    n_rows = max(2, round(total / max_w))
    per = -(-n // n_rows)  # 每行步数（向上取整）
    rows = [list(range(i, min(i + per, n))) for i in range(0, n, per)]
    # 末行只剩一个节点时从上一行搬一个下来，别孤零零挂着
    if len(rows) > 1 and len(rows[-1]) == 1:
        prev = rows[-2]
        if len(prev) > 1:
            rows[-1].insert(0, prev.pop())
    return rows


def _build_horizontal(spec: dict[str, Any]) -> tuple[str, float, float]:
    steps, sides = spec["steps"], spec["side"]
    boxed = spec["box"]
    under_gap = UNDER_GAP if boxed else 4.0
    widths = [_step_w(s, boxed) for s in steps]
    heights = [_step_h(s, boxed) for s in steps]
    # 条件文字窄栏折成 2~3 行居中写在箭头上方（和卷面一致）：
    # 「CH2(COOH)2、吡啶、」/「△」。限宽太大会把每段箭头撑得很长，
    # 三步流程总宽一超就得折行，排成难看的 L 形。
    labels = [_wrap(s["label"], ARROW_LABEL_SIZE, 64) for s in steps]
    under_w = [_under_width(s) for s in steps]
    # 每段箭头的长度按它自己的条件文字算。以前取全局最大值，结果一条
    # 「过量 NaOH 溶液」把所有箭头都拉成 117pt，整张图白白宽了一倍。
    gaps: list[float] = [0.0] * len(steps)
    over = [max(0.0, (under_w[i] - widths[i]) / 2) for i in range(len(steps))]
    for i in range(1, len(steps)):
        lines = labels[i] or []
        need = _text_width(max(lines, key=len), ARROW_LABEL_SIZE) + 22 if lines else 0.0
        # 节点下方的注释比节点还宽时，两边的溢出会盖到邻居，间距至少容得下
        gaps[i] = min(max(GAP_H, need, over[i - 1] + over[i] + 14), 150)

    rows = _plan_rows(widths, gaps, MAX_ROW_W)
    multi = len(rows) > 1
    # 单行时首尾节点的注释可以溢出画布边距；折行后按每行首节点的溢出让位
    lead = 0.0 if multi else over[0]
    tail = 0.0 if multi else over[-1]
    row_lead = [0.0 if not multi else over[row[0]] for row in rows]
    row_ws = [sum(widths[i] for i in row) + sum(gaps[i] for i in row[1:]) for row in rows]
    total_w = max([row_ws[r] + row_lead[r] for r in range(len(rows))]) + lead + tail + MARGIN * 2

    side_node_h = _step_h({"name": "", "smiles": ""}, boxed)
    side_rows = {side["after"] - 1 for side in sides}
    parts: list[str] = []
    centers: dict[int, tuple[float, float]] = {}
    bottoms: dict[int, float] = {}
    row_cy: list[float] = []
    row_bottom: list[float] = []
    row_side: list[float] = []
    y = MARGIN + 16
    for r, row in enumerate(rows):
        row_h = max(heights[i] for i in row)
        row_under = max(_under_height(steps[i]) for i in row)
        # 这一行有支线的话，行底还得给支线让出位置，否则会压到下一行
        side_extra = (SIDE_GAP + side_node_h + 16 + under_gap) if any(i in side_rows for i in row) else 0.0
        x0 = MARGIN + lead if not multi else MARGIN + row_lead[r]
        cy = y + row_h / 2
        x = x0
        for k, i in enumerate(row):
            w, h = widths[i], heights[i]
            top = cy - h / 2
            parts.append(_node(x, top, w, h, steps[i]["name"], steps[i]["smiles"], boxed))
            parts.append(_under(x + w / 2, top + h, steps[i], under_gap))
            centers[i] = (x + w / 2, cy)
            bottoms[i] = top + h
            if k:
                ax1 = x - gaps[i]
                parts.append(_arrow(ax1, cy, x, cy))
                lines = labels[i] or []
                if lines:
                    base_y = cy - 6 - (len(lines) - 1) * 12
                    for j, line in enumerate(lines):
                        parts.append(_label(ax1 + gaps[i] / 2, base_y + j * 12, line))
            x += w + (gaps[i + 1] if i + 1 < len(widths) else 0.0)
        row_cy.append(cy)
        row_bottom.append(cy + row_h / 2 + row_under)
        row_side.append(side_extra)
        y = row_bottom[-1] + side_extra + ROW_GAP

    # 行与行之间用折线接回去，下一行的"进入条件"就写在这条折线上
    for r in range(len(rows) - 1):
        last, first = rows[r][-1], rows[r + 1][0]
        # 该节点自己带支线时，主线从左侧分出去，别和支线挤在同一个点上
        x1 = centers[last][0] - (22 if last in side_rows else 0)
        x2 = centers[first][0]
        # 起点接在节点下方文字之后（而不是整行最底部），线才不会断一大截
        skip_last = under_gap + _under_height(steps[last]) if _under_lines(steps[last]) else 0.0
        y1 = bottoms[last] + skip_last
        y2 = row_cy[r + 1] - heights[first] / 2
        mid = (y1 + y2) / 2
        parts.append(_elbow(x1, y1, x2, y2, mid))
        lines = labels[first] or []
        if lines:
            base = mid - 5 - (len(lines) - 1) * 12
            if abs(x1 - x2) < 0.5:
                # 两行的节点正好上下对齐时连线是竖直的，标注居中会被箭头从中间劈开
                # ——偏到线的左侧（竖排同款处理）；左边不够放就放右侧
                lab_w = max(_text_width(ln, ARROW_LABEL_SIZE) for ln in lines)
                if x1 - lab_w - 12 >= MARGIN:
                    lx, anchor = x1 - 7, "end"
                else:
                    lx, anchor = x1 + 7, "start"
                    total_w = max(total_w, x1 + 7 + lab_w + MARGIN)
            else:
                lx, anchor = (x1 + x2) / 2, "middle"
            for j, line in enumerate(lines):
                parts.append(_label(lx, base + j * 12, line, anchor=anchor))

    for side in sides:
        idx = side["after"] - 1
        # 支线从节点底部偏右一点引出：正中间容易被该步的 note / 结构式名占住
        side_x = centers[idx][0] + 12
        # 该步底下还写着物质名 / 注释时，支线得从那些字下面才开始，否则箭头穿过文字
        skip = under_gap + _under_height(steps[idx]) if _under_lines(steps[idx]) else 0.0
        top = bottoms[idx] + skip
        w, sh = _step_w(side, boxed), _step_h(side, boxed)
        box_top = top + SIDE_GAP
        parts.append(_arrow(side_x, top, side_x, box_top))
        parts.append(_node(side_x - w / 2, box_top, w, sh, side["name"], "", boxed))
        if side["label"]:
            parts.append(_label(side_x + 4, top + SIDE_GAP / 2 + 3, side["label"], anchor="start"))
    return "".join(parts), total_w, y - ROW_GAP + MARGIN


def _build_vertical(spec: dict[str, Any]) -> tuple[str, float, float]:
    steps, sides = spec["steps"], spec["side"]
    boxed = spec["box"]
    under_gap = UNDER_GAP if boxed else 4.0
    box_w = max([BOX_MIN_W] + [_step_w(s, boxed) for s in steps])
    heights = [_step_h(s, boxed) for s in steps]
    # 竖排时标注写箭头左侧、物质名写箭头右侧，两边分列才不会被竖直箭头劈开
    label_lines = [_wrap(s["label"], ARROW_LABEL_SIZE, 96) for s in steps]
    label_w = max(
        (_text_width(line, ARROW_LABEL_SIZE) for lines in label_lines for line in lines),
        default=0.0,
    )
    left = MARGIN + (label_w + 7 if label_w else 0.0)
    cx = left + box_w / 2

    parts: list[str] = []
    y = MARGIN
    last_bottom = MARGIN
    tops: list[tuple[float, float]] = []
    for i, step in enumerate(steps):
        h = heights[i]
        if i:
            # 上一步的下方文字就落在这一段留白里，箭头不够长就会穿字
            gap = GAP_V + _under_height(steps[i - 1])
            top = y - gap
            parts.append(_arrow(cx, top, cx, y))
            lines = label_lines[i]
            if lines:
                base = top + (gap - (len(lines) - 1) * 12) / 2 + 4
                for j, line in enumerate(lines):
                    parts.append(_label(cx - 7, base + j * 12, line, anchor="end"))
        tops.append((y, h))
        parts.append(_node(left, y, box_w, h, step["name"], step["smiles"], boxed))
        parts.append(_under(cx + 7, y + h, step, under_gap, anchor="start"))
        last_bottom = y + h + under_gap + _under_height(step)
        y = last_bottom + GAP_V

    total_h = last_bottom + MARGIN
    total_w = left + box_w + MARGIN
    under_w = max((_under_width(s) for s in steps), default=0.0)
    total_w = max(total_w, cx + 7 + under_w + MARGIN)

    for side in sides:
        idx = side["after"] - 1
        top, h = tops[idx]
        side_cx = left + box_w
        side_cy = top + h / 2
        side_step = {"name": side["name"], "smiles": ""}
        w, sh = _step_w(side_step, boxed), _step_h(side_step, boxed)
        parts.append(_arrow(side_cx, side_cy, side_cx + SIDE_GAP, side_cy))
        parts.append(_node(side_cx + SIDE_GAP, side_cy - sh / 2, w, sh, side["name"], "", boxed))
        total_w = max(total_w, side_cx + SIDE_GAP + w + MARGIN)
    return "".join(parts), total_w, total_h


def _wrap_svg(body: str, w: float, h: float) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" '
        f'width="{w:.0f}" height="{h:.0f}" preserveAspectRatio="xMidYMid meet">'
        "<defs><marker id=\"flow-arrow\" viewBox=\"0 0 10 10\" refX=\"9\" refY=\"5\" "
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#000"/></marker></defs>'
        f"{body}</svg>"
    )


def render(raw: Any, use_cache: bool = True, warns: list[str] | None = None) -> str:
    """结构化描述 → SVG 字符串。"""
    spec = normalise(raw, warns=warns)
    if not spec:
        raise FlowError("流程图为空")
    key = hashlib.sha1(
        (LAYOUT_VER + "|" + json.dumps(spec, ensure_ascii=False, sort_keys=True)).encode()
    ).hexdigest()[:20]
    cached = CACHE / f"flow_{key}.svg"
    if use_cache and cached.exists():
        return cached.read_text(encoding="utf-8")

    body, w, h = _build_vertical(spec) if spec["direction"] == "v" else _build_horizontal(spec)
    svg = _wrap_svg(body, w, h)
    if use_cache:
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            cached.write_text(svg, encoding="utf-8")
        except OSError:
            pass
    return svg


def looks_like_flow(raw: Any) -> bool:
    """粗判这段内容像不像流程图描述（供解析器区分 JSON 对象用途）。"""
    if isinstance(raw, dict):
        return "steps" in raw
    if isinstance(raw, str):
        return bool(re.search(r'"steps"\s*:', raw))
    return False
