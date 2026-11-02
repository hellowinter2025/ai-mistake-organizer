"""体检并修复模型输出留下的毛病（存量库，可反复跑）。

AI 的 JSON 看着能解析，不等于内容能用。实测踩到过的：
- ``figure_tikz`` 被 max_tokens 截断：``\\end{tikzpicture`` 少个花括号，
  或者半条 ``\\draw`` 卡在中间 —— 这张图就永远渲染不出来，只能回退贴原图；
- 同一段坏 TikZ 已经在库里躺了很久，每次渲染都要重新失败一次；
- 正文里残留 Markdown 代码围栏、``\\begin{tikzpicture}`` 片段、不成对的 ``$``，
  前端 KaTeX 会报红；
- SMILES 不合法 / flow JSON 坏掉，渲染层每次都要重试一遍降级。

用法：
    python tools/repair_ai_outputs.py            # 只体检，列出会改什么
    python tools/repair_ai_outputs.py --apply    # 真正写回数据库
    python tools/repair_ai_outputs.py --apply --no-render   # 只改代码不重编译
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.ai import parser  # noqa: E402

TEXT_FIELDS = ("stem", "answer", "error_reason", "solution_fix", "solution_alt", "notes")


def _text_patch(row: dict) -> tuple[dict, list[str]]:
    patch: dict = {}
    notes: list[str] = []
    for key in TEXT_FIELDS:
        raw = row.get(key) or ""
        if not raw.strip():
            continue
        value, warns = parser.normalise_latex(raw)
        # 正文里混进代码块：整段删掉，留着只会在试卷上印出一堆反斜杠
        if "\\begin{tikzpicture}" in value or "```" in value:
            value = _strip_code_leaks(value)
            warns.append("正文里残留代码块，已清除")
        if value != raw:
            patch[key] = value
            notes.extend(f"{key}: {w}" for w in warns)
    return patch, notes


def _strip_code_leaks(text: str) -> str:
    import re

    text = re.sub(r"```[a-zA-Z]*.*?```", "", text, flags=re.S)
    text = re.sub(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", "", text, flags=re.S)
    text = re.sub(r"\\begin\{tikzpicture\}.*$", "", text, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _tikz_patch(row: dict, apply_render: bool) -> tuple[dict, list[str], str]:
    """返回 (补丁, 说明, 渲染结果描述)。"""
    from app.render import tikz as tikz_mod

    patch: dict = {}
    notes: list[str] = []
    raw = str(row.get("figure_tikz") or "").strip()
    if not raw:
        return patch, notes, ""

    try:
        code, fixes = tikz_mod.repair_environment(raw)
    except tikz_mod.TikzError as exc:
        return patch, [f"TikZ 无法修复：{exc}"], ""
    if code != raw:
        patch["figure_tikz"] = code
        notes.append("TikZ 已补全：" + "、".join(fixes))

    if not apply_render:
        return patch, notes, ""

    need_render = code != raw or not row.get("figure_svg")
    if not need_render:
        return patch, notes, "已有图，跳过"
    try:
        result = tikz_mod.render(code)
    except tikz_mod.TikzError as exc:
        return patch, notes + [f"仍然编译失败：{exc}"], "失败"
    from app.render.svg import sanitize_svg

    svg = sanitize_svg(result.svg)
    if not svg:
        return patch, notes + ["编译结果未通过安全校验"], "失败"
    patch["figure_svg"] = svg
    return patch, notes, f"渲染成功 {result.ms}ms"


def _figure_format_issues(row: dict) -> list[str]:
    """SMILES / flow 这类结构化字段能不能用。"""
    from app.render import chem, flow

    out: list[str] = []
    smiles = row.get("figure_smiles")
    if smiles:
        raw = smiles
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {"结构式": raw.strip()}
        try:
            if not chem.normalise(raw):
                out.append("SMILES 无法解析")
        except Exception:  # noqa: BLE001
            out.append("SMILES 无法解析")
    raw_flow = row.get("figure_flow")
    if raw_flow:
        try:
            flow.normalise(json.loads(raw_flow) if isinstance(raw_flow, str) else raw_flow)
        except Exception:  # noqa: BLE001
            out.append("流程图结构不合法")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回数据库（默认只体检）")
    ap.add_argument("--no-render", action="store_true", help="不重新编译 TikZ")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 道（调试用）")
    args = ap.parse_args()

    rows = [dict(r) for r in db.query("SELECT * FROM questions WHERE deleted=0 ORDER BY id")]
    if args.limit:
        rows = rows[: args.limit]
    print(f"检查 {len(rows)} 道题（{'写回' if args.apply else '只体检'}）")

    changed = 0
    problems = 0
    for row in rows:
        uid = str(row["uid"])[:8]
        patch: dict = {}
        notes: list[str] = []

        text_patch, text_notes = _text_patch(row)
        patch.update(text_patch)
        notes.extend(text_notes)

        tikz_patch, tikz_notes, render_state = _tikz_patch(row, args.apply and not args.no_render)
        patch.update(tikz_patch)
        notes.extend(tikz_notes)

        fmt = _figure_format_issues(row)
        if fmt:
            problems += 1
            notes.extend(fmt)

        if not patch and not notes:
            continue
        if patch:
            changed += 1
        print(f"  · id={row['id']} {uid} {render_state}")
        for n in notes:
            print(f"      - {n}")
        if patch and args.apply:
            db.update_question(int(row["id"]), patch)

    print(f"\n需要改：{changed} 道；格式有问题的字段：{problems} 处")
    if not args.apply:
        print("（体检模式，没有写库；加 --apply 生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
