#!/usr/bin/env python3
"""入库前的内容级自检（补 mm.py validate 管不到的部分）。

mm.py add --dry-run 只校验字段格式与合法性；本脚本查「内容自洽」：
公式定界符、答案与解析结论是否一致、错因与答案是否打架、图形是否复核过、
不确定的地方有没有留痕。

用法：
  python check_batch.py batch.json
  python check_batch.py batch.json --strict   # WARN 也算失败

有 ERROR 时退出码 1，方便 && 串在 add 前面挡住入库。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TEXT_FIELDS = ("stem", "answer", "error_reason", "solution_fix", "solution_alt", "notes")
CHOICE = ("选择题（单选）", "选择题（多选）")
MARK_WORDS = ("红笔", "对勾", "打叉", "叉号", "老师批", "判错", "判对", "√", "✗")


def strip_tex(s: str) -> str:
    s = re.sub(r"\\(?:dfrac|frac|sqrt|pi|times|cdot|leq|geq|ne)\b", "", s)
    return s.replace("$", "").replace("{", "").replace("}", "").replace("\\", "")


def tail(s: str, n: int = 220) -> str:
    lines = [l for l in s.strip().splitlines() if l.strip()]
    return "\n".join(lines[-3:])[-n:]


def check(q: dict, idx: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    tag = f"第{idx}题"

    for f in TEXT_FIELDS:
        v = q.get(f)
        if not isinstance(v, str):
            continue
        if v.count("$") % 2:
            out.append(("ERROR", f"{f} 里 $ 数量为奇数，页面公式会显示成红字"))
        if re.search(r"\\[nrt](?![a-z])", v):
            out.append(("ERROR", f"{f} 里有字面量 \\n / \\r / \\t，要写成真换行"))

    stem = str(q.get("stem") or "")
    if any(w in stem for w in MARK_WORDS):
        out.append(("ERROR", "stem 里混进了批改痕迹的描述，只抄题目正文"))
    qtype = str(q.get("question_type") or "")
    if qtype.startswith("选择题") and "（　　）" not in stem and "(  )" not in stem:
        out.append(("WARN", "选择题 stem 里没有「（　　）」占位"))

    ans = str(q.get("answer") or "").strip()
    fix = str(q.get("solution_fix") or "")
    if not ans:
        out.append(("ERROR", "answer 为空"))
    if qtype in CHOICE:
        letters = re.sub(r"[^A-Za-z]", "", ans).upper()
        if qtype == "选择题（单选）" and len(letters) > 1:
            out.append(("ERROR", f"单选却给了 {len(letters)} 个字母：{letters}"))
        if letters and letters != "".join(sorted(set(letters))):
            out.append(("WARN", f"多选答案 {letters} 未按字母序去重"))
        if letters and letters not in tail(fix).upper().replace(" ", ""):
            out.append(("WARN", f"solution_fix 末段没有出现答案 {letters}（末句结论要和 answer 一致，也核对顿号写法）"))
    elif ans and len(ans) <= 8 and "\\" not in ans and strip_tex(ans) not in strip_tex(tail(fix)):
        out.append(("WARN", f"solution_fix 末段没找到答案 {ans!r}，核对结论是否一致"))

    reason = str(q.get("error_reason") or "")
    if not reason:
        out.append(("ERROR", "error_reason 为空"))
    elif "粗心" in reason or len(reason) < 20:
        out.append(("WARN", "error_reason 太笼统，要落到方法/思路/概念层面"))
    if qtype == "选择题（多选）":
        mentioned = re.findall(r"(?:漏选|误选|多选|没选|排除)\s*([A-D])", reason)
        bad = [c for c in mentioned if c not in ans.upper()]
        if bad:
            out.append(("WARN", f"错因提到的 {','.join(bad)} 不在答案 {ans} 里，核对错因是否写反"))

    kp = q.get("knowledge_points") or []
    if isinstance(kp, str):
        kp = [x for x in re.split(r"[、,，;；]", kp) if x.strip()]
    if not 2 <= len(kp) <= 4:
        out.append(("WARN", f"knowledge_points 有 {len(kp)} 个，口径要求 2~4 个"))

    subj = str(q.get("subject") or "")
    if subj in ("数学", "物理") and fix:
        lines = fix.splitlines()
        if not lines or not lines[0].startswith("方法："):
            out.append(("ERROR", "数学/物理的 solution_fix 必须以「方法：」开头"))
        if len(lines) < 2 or not lines[1].startswith("触发点："):
            out.append(("ERROR", "solution_fix 第二行必须以「触发点：」开头"))

    try:
        d = int(q.get("difficulty") or 3)
        if not 1 <= d <= 5:
            out.append(("ERROR", f"difficulty={d} 超出 1~5"))
    except (TypeError, ValueError):
        out.append(("ERROR", "difficulty 不是整数"))

    tikz = str(q.get("figure_tikz") or "")
    if tikz:
        if q.get("needs_figure"):
            out.append(("ERROR", "同时给了 figure_tikz 和 needs_figure=1，重绘的图不会被用到"))
        if not q.get("_figure_checked"):
            out.append(("WARN", "有 figure_tikz 但没标 _figure_checked：先 render + 打开 PNG 核对，再置 true"))
    elif q.get("needs_figure") and not q.get("figure_box"):
        out.append(("ERROR", "needs_figure=1 但没有 figure_box，导出会贴整段题干截图"))

    if not q.get("crop_token") and "截图" not in str(q.get("notes") or ""):
        out.append(("WARN", "没有 crop_token，且 notes 未说明原题截图缺失"))

    notes = str(q.get("notes") or "")
    if "□" in stem + ans or "□" in fix:
        if "□" not in notes and "看不清" not in notes:
            out.append(("ERROR", "题干/答案用了 □ 占位，但 notes 没说明"))

    return [(lvl, f"{tag} {msg}") for lvl, msg in out]


def main() -> int:
    ap = argparse.ArgumentParser(description="入库前内容自检")
    ap.add_argument("batch", help="batch.json（数组或含 items 的对象）")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    args = ap.parse_args()

    data = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list) or not data:
        raise SystemExit("batch.json 必须是非空数组")

    findings: list[tuple[str, str]] = []
    for i, q in enumerate(data, 1):
        findings.extend(check(q, i))

    for lvl, msg in findings:
        print(f"[{lvl}] {msg}")
    ne = sum(1 for l, _ in findings if l == "ERROR")
    nw = sum(1 for l, _ in findings if l == "WARN")
    if not findings:
        print(f"✓ {len(data)} 道题内容自检全部通过")
    else:
        print(f"共 {len(data)} 道题：ERROR {ne} / WARN {nw}")
    if ne or (args.strict and nw):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
