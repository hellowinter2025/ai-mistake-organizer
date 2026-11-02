"""覆盖各类公式形态的测试素材（前后端共用）。"""

INLINE = [
    r"$x^2$",
    r"$a_{n+1}=2a_n+1$",
    r"$\frac{1}{2}$",
    r"$\dfrac{a+b}{c-d}$",
    r"$\sqrt{2}$",
    r"$\sqrt[3]{x+1}$",
    r"$x \in \mathbb{R}$",
    r"$|x-1| \le 2$",
    r"$\angle ABC = 60^\circ$",
    r"$\triangle ABC \sim \triangle DEF$",
    r"$a \perp b$，$c \parallel d$",
    r"$\overrightarrow{AB}=\vec{a}+\vec{b}$",
    r"$\sin^2\alpha+\cos^2\alpha=1$",
    r"$\lim_{x\to 0}\frac{\sin x}{x}=1$",
    r"$f'(x)=3x^2-2x$",
    r"$\int_0^1 x^2\,\mathrm{d}x=\frac{1}{3}$",
    r"$\sum_{k=1}^{n} k = \frac{n(n+1)}{2}$",
    r"$\binom{n}{k}=\frac{n!}{k!(n-k)!}$",
    r"$C_n^k = C_n^{n-k}$",
    r"$P(A\mid B)=\frac{P(AB)}{P(B)}$",
    r"$\log_2 8 = 3$",
    r"$\ln e = 1$",
    r"$A\cup B$，$A\cap B$，$A\subseteq B$",
    r"$x \neq 0$ 且 $x \ge -1$",
    r"$\text{当 } x>0 \text{ 时}$",
    # 2026-10-31 实拍问题：\text 被折行吃掉反斜杠 → 页面显示 "text平面"
    r"$\text{平面 } AMC \perp \text{平面 } A'MC'$",
    # 同批问题：\sqrt 丢了反斜杠 → 分子显示成 "sqrt2"
    r"$BD=\frac{1}{2}AC=\frac{\sqrt{2}}{2}$",
    r"$\{a_n\}$ 是等差数列",
    r"$\varnothing \subsetneq A$",
    r"$\overline{z}=a-b\mathrm{i}$",
    r"$60\%$ 的学生",
    r"$\left(\frac{a}{b}\right)^n$",
]

DISPLAY = [
    r"$$\frac{a}{b}+\frac{c}{d}=\frac{ad+bc}{bd}$$",
    r"$$\begin{aligned} f(x) &= x^2-4x+3 \\ &= (x-2)^2-1 \end{aligned}$$",
    r"$$\begin{cases} x^2+2x, & x \le 0 \\ \dfrac{1}{x}, & x>0 \end{cases}$$",
    r"$$\begin{vmatrix} 1 & 2 & 3 \\ 2 & 5 & 3 \\ 1 & 0 & 8 \end{vmatrix} = 1$$",
    r"$$\begin{pmatrix} a & b \\ c & d \end{pmatrix}$$",
    r"$$S_n=\sum_{k=1}^{n}\frac{1}{k(k+1)}=1-\frac{1}{n+1}$$",
    r"$$\int_{-\infty}^{+\infty} e^{-x^2}\,\mathrm{d}x=\sqrt{\pi}$$",
    r"$$\lim_{n\to\infty}\left(1+\frac{1}{n}\right)^n=e$$",
    r"$$\vec{a}\cdot\vec{b}=|\vec{a}||\vec{b}|\cos\theta$$",
    r"$$\begin{array}{c|ccc} x & 0 & 1 & 2 \\ \hline y & 3 & 0 & -1 \end{array}$$",
    r"$$\sqrt{\frac{a+\sqrt{b}}{2}}$$",
    r"$$f(x)=\begin{cases} 1, & x\in\mathbb{Q} \\ 0, & x\notin\mathbb{Q} \end{cases}$$",
    r"$$\frac{\partial^2 u}{\partial x^2}+\frac{\partial^2 u}{\partial y^2}=0$$",
    r"$$\left\{ \begin{aligned} x+y&=1 \\ x-y&=3 \end{aligned} \right.$$",
    r"$$\underset{n\to\infty}{\lim} a_n = A$$",
    r"$$\overset{\frown}{AB}$$",
]

EDGE = [
    # 中文与公式混排
    r"设集合 $A=\{1,2,3\}$，$B=\{x\mid x>1\}$，则 $A\cap B=$（  ）",
    # 多段公式同行
    r"由 $a>0$，$b>0$ 得 $a+b\ge 2\sqrt{ab}$。",
    # 转义美元
    r"价格为 \$5，公式为 $x+1$。",
    # 公式内含下标文字
    r"$v_{\text{初}}=3\,\mathrm{m/s}$",
    # 化学式
    r"$\mathrm{2H_2+O_2 \to 2H_2O}$",
    # 长公式换行
    r"$$(a+b)^n=\sum_{k=0}^{n}\binom{n}{k}a^{n-k}b^{k}$$",
    # 带单位的物理量
    r"$g=9.8\,\mathrm{m/s^2}$",
    # 角度与度数符号
    r"$\theta = 30^{\circ}$",
]

ALL = INLINE + DISPLAY + EDGE
