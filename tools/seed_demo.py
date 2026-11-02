"""生成一组覆盖各种公式形态的测试题目（用于验证渲染与导出）。

用法：.venv/Scripts/python.exe tools/seed_demo.py [--clean]
"""
from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8765"

QUESTIONS = [
    {
        "stem": r"已知函数 $f(x)=x^{2}-4x+3$，求它在闭区间 $[0,3]$ 上的最大值与最小值。",
        "answer": r"最大值为 $f(0)=3$，最小值为 $f(2)=-1$。",
        "error_reason": r"直接把顶点纵坐标当作最小值，没有验证对称轴 $x=2$ 是否落在区间内。",
        "solution_fix": r"""配方得
$$f(x)=x^{2}-4x+3=(x-2)^{2}-1$$
抛物线开口向上，对称轴为 $x=2\in[0,3]$，故
$$f_{\min}=f(2)=-1,\qquad f_{\max}=\max\{f(0),f(3)\}=3$$""",
        "solution_alt": r"利用导数：$f'(x)=2x-4$，令 $f'(x)=0$ 得 $x=2$，比较端点与驻点函数值即可。",
        "knowledge_points": ["二次函数", "闭区间最值", "配方法"],
        "difficulty": 3,
        "question_type": "解答题",
    },
    {
        "stem": r"计算定积分 $\displaystyle\int_{0}^{1}\left(3x^{2}+2x\right)\mathrm{d}x$。",
        "answer": r"$2$",
        "error_reason": r"原函数求错，把 $3x^{2}$ 的原函数写成 $x^{2}$。",
        "solution_fix": r"""$$\int_{0}^{1}(3x^{2}+2x)\,\mathrm{d}x=\left[x^{3}+x^{2}\right]_{0}^{1}=1+1=2$$""",
        "solution_alt": r"",
        "knowledge_points": ["定积分", "牛顿-莱布尼茨公式"],
        "difficulty": 2,
        "question_type": "填空题",
    },
    {
        "stem": r"""已知数列 $\{a_n\}$ 满足 $a_n=\dfrac{1}{n(n+1)}$，求前 $n$ 项和 $S_n$。""",
        "answer": r"$S_n=\dfrac{n}{n+1}$",
        "error_reason": r"裂项后没有检查首尾相消的剩余项，多留了一项。",
        "solution_fix": r"""由 $\dfrac{1}{n(n+1)}=\dfrac{1}{n}-\dfrac{1}{n+1}$，累加得
$$S_n=\left(1-\frac{1}{2}\right)+\left(\frac{1}{2}-\frac{1}{3}\right)+\cdots+\left(\frac{1}{n}-\frac{1}{n+1}\right)=1-\frac{1}{n+1}=\frac{n}{n+1}$$""",
        "solution_alt": r"用数学归纳法验证：$n=1$ 时成立，假设 $S_k=\dfrac{k}{k+1}$，则 $S_{k+1}=S_k+a_{k+1}=\dfrac{k+1}{k+2}$。",
        "knowledge_points": ["数列求和", "裂项相消"],
        "difficulty": 3,
        "question_type": "解答题",
    },
    {
        "stem": r"在 $\triangle ABC$ 中，$AB=AC=5$，$BC=6$，求 $\triangle ABC$ 的面积及 $\cos A$。",
        "answer": r"$S=12$，$\cos A=\dfrac{7}{25}$",
        "error_reason": r"作高后忘记 $BD=\dfrac{1}{2}BC=3$，把 $BD$ 当成 $6$ 计算。",
        "solution_fix": r"""作 $AD\perp BC$ 于 $D$，由等腰三角形性质 $BD=3$，故
$$AD=\sqrt{AB^{2}-BD^{2}}=\sqrt{25-9}=4,\qquad S=\frac{1}{2}\times 6\times 4=12$$
又由余弦定理
$$\cos A=\frac{AB^{2}+AC^{2}-BC^{2}}{2\cdot AB\cdot AC}=\frac{25+25-36}{50}=\frac{7}{25}$$""",
        "solution_alt": r"用面积公式 $S=\dfrac{1}{2}AB\cdot AC\sin A$ 先求 $\sin A=\dfrac{24}{25}$，再由 $\sin^{2}A+\cos^{2}A=1$ 得 $\cos A=\dfrac{7}{25}$（注意 $A$ 为锐角）。",
        "knowledge_points": ["解三角形", "余弦定理", "等腰三角形"],
        "difficulty": 2,
        "question_type": "解答题",
    },
    {
        "stem": r"""已知函数 $f(x)=\begin{cases}x^{2}+2x, & x\le 0\\ \dfrac{1}{x}, & x>0\end{cases}$，求 $f(f(-1))$ 的值。""",
        "answer": r"$f(f(-1))=f(-1)=-1$",
        "error_reason": r"内层函数值算错：$f(-1)=1-2=-1$，学生误算为 $1+2=3$。",
        "solution_fix": r"""先算内层：$f(-1)=(-1)^{2}+2\times(-1)=1-2=-1$。
因 $-1\le 0$，仍用第一段：
$$f(f(-1))=f(-1)=(-1)^{2}+2\times(-1)=-1$$""",
        "solution_alt": r"",
        "knowledge_points": ["分段函数", "复合函数"],
        "difficulty": 2,
        "question_type": "填空题",
    },
    {
        "stem": r"""计算行列式 $\begin{vmatrix}1 & 2 & 3\\ 2 & 5 & 3\\ 1 & 0 & 8\end{vmatrix}$ 的值。""",
        "answer": r"$1$",
        "error_reason": r"按第一行展开时余子式的符号写错，第二项应为负号。",
        "solution_fix": r"""按第一行展开：
$$\begin{vmatrix}1&2&3\\2&5&3\\1&0&8\end{vmatrix}
=1\cdot\begin{vmatrix}5&3\\0&8\end{vmatrix}-2\cdot\begin{vmatrix}2&3\\1&8\end{vmatrix}+3\cdot\begin{vmatrix}2&5\\1&0\end{vmatrix}
=40-2\times13+3\times(-5)=1$$""",
        "solution_alt": r"用行变换化为上三角：$r_2-2r_1,\ r_3-r_1$ 后对角线相乘。",
        "knowledge_points": ["行列式", "按行展开"],
        "difficulty": 3,
        "question_type": "填空题",
    },
    {
        "stem": r"""已知 $\sin\alpha=\dfrac{3}{5}$，且 $\alpha\in\left(\dfrac{\pi}{2},\pi\right)$，求 $\sin 2\alpha$ 与 $\cos 2\alpha$。""",
        "answer": r"$\sin 2\alpha=-\dfrac{24}{25}$，$\cos 2\alpha=\dfrac{7}{25}$",
        "error_reason": r"没有根据象限判断 $\cos\alpha<0$，直接把 $\cos\alpha$ 取正。",
        "solution_fix": r"""由 $\alpha$ 在第二象限，$\cos\alpha=-\sqrt{1-\sin^{2}\alpha}=-\dfrac{4}{5}$，于是
$$\sin 2\alpha=2\sin\alpha\cos\alpha=2\times\frac{3}{5}\times\left(-\frac{4}{5}\right)=-\frac{24}{25}$$
$$\cos 2\alpha=1-2\sin^{2}\alpha=1-2\times\frac{9}{25}=\frac{7}{25}$$""",
        "solution_alt": r"也可先求 $\tan\alpha=-\dfrac{3}{4}$，再用万能公式。",
        "knowledge_points": ["三角函数", "二倍角公式", "象限符号"],
        "difficulty": 3,
        "question_type": "解答题",
    },
    {
        "stem": r"""如图，在 $\triangle ABC$ 中，$AB=AC=5$，$BC=6$，$AD\perp BC$ 于 $D$，求 $\triangle ABC$ 的面积。""",
        "answer": r"$12$",
        "error_reason": r"作高后把 $BD$ 误当成 $BC=6$，导致高算成 $\sqrt{25-36}$。",
        "solution_fix": r"""由等腰三角形三线合一，$D$ 为 $BC$ 中点，故 $BD=3$。
$$AD=\sqrt{AB^{2}-BD^{2}}=\sqrt{25-9}=4,\qquad S=\frac{1}{2}BC\cdot AD=\frac{1}{2}\times 6\times 4=12$$""",
        "solution_alt": r"",
        "knowledge_points": ["等腰三角形", "勾股定理", "三角形面积"],
        "difficulty": 2,
        "question_type": "解答题",
        "needs_figure": True,
        "figure_svg": (
            '<svg viewBox="0 0 400 300">'
            '<polygon points="200,30 50,250 350,250" fill="none" stroke="#111" stroke-width="2"/>'
            '<line x1="200" y1="30" x2="200" y2="250" stroke="#111" stroke-width="1.5" stroke-dasharray="5 4"/>'
            '<rect x="200" y="232" width="16" height="16" fill="none" stroke="#111" stroke-width="1"/>'
            '<text x="200" y="20" font-size="17" text-anchor="middle">A</text>'
            '<text x="36" y="268" font-size="17" text-anchor="middle">B</text>'
            '<text x="364" y="268" font-size="17" text-anchor="middle">C</text>'
            '<text x="206" y="272" font-size="15" text-anchor="middle">D</text>'
            '</svg>'
        ),
    },
    {
        "stem": r"设 $x>0$，$y>0$，且 $x+y=1$，求 $\dfrac{1}{x}+\dfrac{4}{y}$ 的最小值。",
        "answer": r"最小值为 $9$",
        "error_reason": r"直接用基本不等式 $\dfrac{1}{x}+\dfrac{4}{y}\ge 2\sqrt{\dfrac{4}{xy}}$，但没有处理 $xy$ 的上界，等号条件不成立。",
        "solution_fix": r"""乘 $1$ 法：
$$\frac{1}{x}+\frac{4}{y}=(x+y)\left(\frac{1}{x}+\frac{4}{y}\right)=5+\frac{y}{x}+\frac{4x}{y}\ge 5+2\sqrt{4}=9$$
当且仅当 $\dfrac{y}{x}=\dfrac{4x}{y}$ 即 $y=2x$，结合 $x+y=1$ 得 $x=\dfrac{1}{3},y=\dfrac{2}{3}$ 时取等号。""",
        "solution_alt": r"消元后化为单变量函数 $g(x)=\dfrac{1}{x}+\dfrac{4}{1-x}$，用导数求最小值。",
        "knowledge_points": ["基本不等式", "乘 1 法"],
        "difficulty": 4,
        "question_type": "解答题",
    },
]


def main() -> int:
    client = httpx.Client(timeout=30, trust_env=False)
    token = client.get(f"{BASE}/api/bootstrap").json()["token"]
    headers = {"X-App-Token": token}

    if "--clean" in sys.argv:
        data = client.get(f"{BASE}/api/questions?limit=300").json()
        demo = [q for q in data["items"] if q.get("notes") == "demo-seed"]
        for q in demo:
            client.delete(f"{BASE}/api/questions/{q['uid']}", headers=headers)
        print(f"已清理 {len(demo)} 道演示题")
        return 0

    created = 0
    for q in QUESTIONS:
        payload = dict(q)
        payload["notes"] = "demo-seed"
        payload["subject"] = "数学"
        res = client.post(f"{BASE}/api/questions", headers=headers, json=payload)
        if res.status_code == 200:
            created += 1
        else:
            print("创建失败", res.status_code, res.text[:200])
    print(f"已创建 {created} 道演示题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
