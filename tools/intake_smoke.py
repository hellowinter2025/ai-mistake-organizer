"""错题录入 skill 的端到端自检。

模拟一张错题照片，走完「上传 → 预览 → 裁剪 → 校验 → 入库 → 查重 → 回读」，
最后把自己录进去的题删掉，不留垃圾。

前提：服务在跑（127.0.0.1:8765）。用法：

    .venv/Scripts/python.exe tools/intake_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MM = Path.home() / ".workbuddy" / "skills" / "错题录入" / "scripts" / "mm.py"
if not MM.is_file():
    MM = ROOT / "skills" / "错题录入" / "scripts" / "mm.py"  # 随仓库分发的布局
TMP = Path(tempfile.gettempdir()) / "mm_intake" / "_smoke"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def mm(*args: str, json_out: bool = True) -> tuple[int, str, str]:
    cmd = [str(PY), str(MM)] + (["--json"] if json_out else []) + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    return proc.returncode, proc.stdout, proc.stderr


def make_page(path: Path) -> dict:
    """画一张「带红笔批改」的模拟试卷：3 道题纵向排开。"""
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int):
        for name in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc"):
            if os.path.exists(name):
                return ImageFont.truetype(name, size)
        return ImageFont.load_default()

    width, height = 1240, 1800
    img = Image.new("RGB", (width, height), (252, 252, 250))
    d = ImageDraw.Draw(img)
    d.text((width // 2 - 150, 60), "高 三 数 学 错 题 整 理", font=font(36), fill="black")

    blocks = [
        (150, ["1. 已知函数 f(x)=x²-4x+3，求它在区间 [0,3] 上的最小值。",
               "   （学生写：f(x)=(x-2)²-1，所以最小值是 1。）"]),
        (620, ["2. 在 △ABC 中，AB=AC=5，BC=6，求 △ABC 的面积。",
               "   （学生写：S=1/2×5×6=15。）"]),
        (1090, ["3. 解方程 2x²-5x+2=0。",
                "   （学生写：Δ=25-16=9，x=(5±3)/4，所以 x=2 或 x=1/2。）"]),
    ]
    spans = []
    for y, lines in blocks:
        for i, text in enumerate(lines):
            d.text((90, y + i * 46), text, font=font(28), fill=(20, 20, 20))
        spans.append((y - 20, y + 46 * len(lines) + 10))

    # 红笔：第 1 题打叉，第 2 题打叉并圈出 15
    d.line([(1010, 150), (1075, 215)], fill=(205, 45, 40), width=7)
    d.line([(1075, 150), (1010, 215)], fill=(205, 45, 40), width=7)
    d.line([(1010, 620), (1075, 685)], fill=(205, 45, 40), width=7)
    d.line([(1075, 620), (1010, 685)], fill=(205, 45, 40), width=7)
    d.ellipse([(770, 676), (990, 722)], outline=(205, 45, 40), width=5)

    img.save(path, "JPEG", quality=92)
    return {
        "path": path,
        "width": width,
        "height": height,
        "spans": [(round(y1 / height, 3), round(y2 / height, 3)) for y1, y2 in spans],
    }


def main() -> int:
    if not MM.is_file():
        print(f"找不到脚本：{MM}")
        return 2
    TMP.mkdir(parents=True, exist_ok=True)
    print(f"脚本：{MM}\n临时目录：{TMP}\n")

    code, out, err = mm("health")
    if code != 0:
        print("服务不可用，先启动：双击 启动.vbs\n" + (err or out))
        return 2
    print("1) 服务连通 ✅")

    page = make_page(TMP / "page.jpg")
    print(f"2) 生成模拟错题照片：{page['path']}  {page['width']}x{page['height']}")

    code, out, err = mm("upload", str(page["path"]))
    if code != 0:
        print(out or err)
        return 1
    image = json.loads(out)["images"][0]
    print(f"3) 上传：image_id={image['id']}  reused={image['reused']}")
    check("上传返回有效 id", len(image["id"]) >= 12, image["id"])

    preview = TMP / "preview.png"
    code, out, err = mm("preview", image["id"], "--out", str(preview), json_out=False)
    check("预览图落盘", preview.is_file() and preview.stat().st_size > 2000, f"{preview.stat().st_size if preview.is_file() else 0} 字节")

    code, out, err = mm("analyze", image["id"])
    cands = json.loads(out).get("candidates") if code == 0 else []
    check("自动切题给出候选", bool(cands), f"{len(cands or [])} 块")

    span_args: list[str] = []
    for y1, y2 in page["spans"]:
        span_args += ["--span", f"{y1}:{y2}"]
    crop_dir = TMP / "crops"
    code, out, err = mm("crop", image["id"], *span_args, "--save-dir", str(crop_dir))
    if code != 0:
        print(out or err)
        return 1
    crops = json.loads(out)["crops"]
    files = [Path(p) for p in json.loads(out)["files"]]
    check("裁出 3 块", len(crops) == 3, f"{len(crops)} 块")
    check("裁图下载到本地", all(f.is_file() and f.stat().st_size > 3000 for f in files), str(crop_dir))
    check("三个 token 互不相同", len({c["token"] for c in crops}) == 3)

    batch = [
        {
            "image_id": image["id"],
            "crop_token": crops[0]["token"],
            "bbox": crops[0]["bbox"],
            "subject": "数学",
            "question_type": "解答题",
            "stem": "已知函数 $f(x)=x^2-4x+3$，求它在区间 $[0,3]$ 上的最小值。",
            "answer": "-1",
            "error_reason": "配方后把顶点处的函数值当成了最小值，没有核对区间端点，把 $x=2$ 处的 $-1$ 写成了 $1$。",
            "solution_fix": (
                "方法：配方法求二次函数在闭区间上的最值\n"
                "触发点：题目给定完整区间且要求最值，先看对称轴是否落在区间内\n"
                "过程：$f(x)=(x-2)^2-1$，对称轴 $x=2\\in[0,3]$，故最小值为 $f(2)=-1$；"
                "端点 $f(0)=3$、$f(3)=0$，所以最大值是 $3$。"
            ),
            "solution_alt": "",
            "knowledge_points": ["二次函数的最值", "配方法"],
            "difficulty": 2,
            "notes": "",
        },
        {
            "image_id": image["id"],
            "crop_token": crops[1]["token"],
            "bbox": crops[1]["bbox"],
            "subject": "数学",
            "question_type": "解答题",
            "stem": "在 $\\triangle ABC$ 中，$AB=AC=5$，$BC=6$，求 $\\triangle ABC$ 的面积。",
            "answer": "12",
            "error_reason": "把等腰三角形的腰长直接当成底边上的高，用了 $\\frac12\\times5\\times6$，忽略了高要另算。",
            "solution_fix": (
                "方法：等腰三角形作底边上的高，用勾股定理\n"
                "触发点：已知两腰相等且给了底边，求面积却没有高\n"
                "过程：作 $AD\\perp BC$ 于 $D$，由等腰得 $BD=3$，"
                "在 $Rt\\triangle ABD$ 中 $AD=\\sqrt{5^2-3^2}=4$，故 $S=\\frac12\\times6\\times4=12$。"
            ),
            "solution_alt": "也可用海伦公式直接算，但高中阶段用高更直观。",
            "knowledge_points": ["等腰三角形的性质", "勾股定理", "三角形面积"],
            "difficulty": 2,
            "notes": "",
        },
    ]
    batch_file = TMP / "batch.json"
    batch_file.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")

    code, out, err = mm("add", "--json", str(batch_file), "--dry-run")
    if code != 0:
        print(out or err)
        return 1
    dry = json.loads(out)
    check("预演全部通过", all(r["status"] == "dry-run" for r in dry), f"{len(dry)} 道")

    code, out, err = mm("add", "--json", str(batch_file))
    if code != 0:
        print(out or err)
        return 1
    created = [r for r in json.loads(out) if r["status"] == "created"]
    # 收集**所有**入库成功的题（含后面查重失手多录的），清理时一个都不能漏
    created_all: list[dict] = list(created)
    check("正式入库 2 道", len(created) == 2, "、".join(f"#{r['code']}" for r in created))
    for row in created:
        for msg in row.get("warnings") or []:
            print(f"     提醒：{msg}")

    code, out, err = mm("add", "--json", str(batch_file))
    again = json.loads(out) if code == 0 else []
    created_all += [r for r in again if r["status"] == "created"]
    check("同一批再导一次会被查重", all(r["status"] == "duplicate" for r in again), f"{len(again)} 道")

    if created:
        code, out, err = mm("get", created[0]["code"])
        ok = code == 0 and "最小值" in out
        check("按编号回读", ok)

    # 清理：只删本次自检录进去的
    import httpx

    token = (ROOT / "data" / ".session_token").read_text(encoding="utf-8").strip()
    client = httpx.Client(
        base_url="http://127.0.0.1:8765", trust_env=False, headers={"X-App-Token": token}, timeout=30
    )
    for row in created_all:
        client.delete(f"/api/questions/{row['uid']}")
    client.close()
    print(f"8) 已清理 {len(created_all)} 道自检题")

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过：录入链路可用（upload → preview → crop → add → get）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
