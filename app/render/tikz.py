"""TikZ → SVG 渲染。

模型画图手写 SVG 坐标是最容易翻车的一环：它得自己在 400×300 像素里换算，
标签位置靠估，曲线只能用折线凑。改成让模型写 TikZ（世界坐标 + 语义定位），
由本地的 node-tikzjax（TeX 引擎编成 WebAssembly）编译成 SVG，质量差一个档次。

设计要点：
- 常驻 node 进程（``tools/tikz_server.cjs``）：TeXT 引擎加载一次约 500ms，
  每题单起进程等于白付这笔开销；
- TeX 引擎是单例，调用必须串行，这里用锁保证；
- 超时/进程崩了就杀掉重来，绝不把坏状态留给下一题；
- 渲染结果按代码哈希缓存，重复渲染同一张图不再编译；
- 只接受 tikzpicture 主体：document 环境与宏包由服务端拼，模型碰不到 preamble。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CACHE_DIR

RENDER_DIR = CACHE_DIR / "tikz"
ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "tools" / "tikz_server.cjs"
# TikZ 出的 SVG 里文字用 TeX 字体（cmr10 等），前端要有对应 @font-face 才显示得对
FONT_DIR = ROOT / "web" / "vendor" / "tikz"
FONT_SRC = ROOT / "node_modules" / "node-tikzjax" / "css"

MAX_CODE = 8000
DEFAULT_TIMEOUT = 30.0

# 这些命令能让 TeX 读写文件、跑 shell、改字符表，或者干脆死循环。
# \foreach 是 TikZ 常用语法，放行；真要卡死由超时兜底。
_BANNED = re.compile(
    r"\\(?:write18|input|include|includeonly|openout|openin|read|write|immediate|"
    r"special|directlua|usepackage|RequirePackage|documentclass|catcode|makeatletter|"
    r"makeatother|csname|endcsname|shipout|loop|repeat|newread|newwrite)\b"
)

_FENCE = re.compile(r"^\s*```+[a-zA-Z]*\s*|\s*```+\s*$")
_TIKZ_BLOCK = re.compile(r"\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}", re.S)


class TikzError(RuntimeError):
    """渲染失败：调用方据此降级成「贴原题截图」。"""


@dataclass
class RenderResult:
    svg: str
    cached: bool
    ms: int
    code: str = ""  # 修好之后实际编译的代码（有改动时调用方应回写）
    fixes: list[str] = field(default_factory=list)  # 补了什么，用于提示用户


def ensure_fonts() -> None:
    """把 TeX 字体同步到前端静态目录（只做一次）。

    字体有 3.8MB，不适合入库；``node_modules`` 又是 npm install 生成的。
    所以首次渲染时自动拷一份，克隆仓库后不用手动搬。
    """
    if (FONT_DIR / "fonts.css").exists() or not FONT_SRC.exists():
        return
    try:
        shutil.copytree(FONT_SRC, FONT_DIR, dirs_exist_ok=True)
    except OSError:
        pass  # 拷不过去也不影响编译，只是字形退化


def _close_options(rest: str) -> tuple[str, bool]:
    """``\\begin{tikzpicture}[scale=0.75`` → 在第一行末尾补上没写完的 ``]``。"""
    head, sep, tail = rest.partition("\n")
    if "[" not in head or "]" in head:
        return rest, False
    if not head.lstrip().startswith("["):
        # [ 出现在命令中间（比如 \draw [->]），不是环境选项，别乱补
        return rest, False
    return head + "]" + sep + tail, True


_DOLLAR_WRAP = re.compile(r"^\s*(?:\$\$?|\\\[)\s*|\s*(?:\$\$?|\\\[)\s*$")
_END_TAIL = re.compile(r"\\(?:end|en|e)?\s*\{?\s*tikzpictur[a-z]*\}?\s*$", re.I)

# TeX 引擎（pdftex + 基础宏包）没有中文字体，代码里出现一个汉字就是
# "! Package inputenc Error: Unicode character ..." 整张图编译失败。
_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff01-\uff60\uffe0-\uffe6]+")


def _trim_to_statement(body: str) -> tuple[str, str]:
    """把画图主体裁到最后一个"完整的语句"为止（应对输出被截断）。

    TeX 遇到 ``\\draw (A)--(B`` 这种半句会整体编译失败，与其整张图丢掉，
    不如把没写完的最后一行切掉——丢一笔总比丢整题强。
    返回 (主体, 做了什么)：``""`` / ``"semicolon"`` / ``"trimmed"``。
    """
    depth = par = brk = 0
    last_safe = 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "(":
            par += 1
        elif ch == ")":
            par = max(0, par - 1)
        elif ch == "[":
            brk += 1
        elif ch == "]":
            brk = max(0, brk - 1)
        if depth == 0 and par == 0 and brk == 0 and ch == ";":
            last_safe = i + 1
    tail = body.rstrip()
    if depth == 0 and par == 0 and brk == 0:
        # 语法完整，只差一个分号（模型常忘）
        if tail and not tail.endswith(";"):
            return tail + ";\n", "semicolon"
        return body, ""
    if last_safe:
        return body[:last_safe] + "\n", "trimmed"
    return "", "trimmed"


def repair_environment(code: str) -> tuple[str, list[str]]:
    """把模型给的 TikZ 补成**能编译**的一段 tikzpicture，返回 (代码, 修了什么)。

    真实响应里见过的问题，全部在这里兜住：
    1. 输出被 max_tokens 截断 → ``\\end{tikzpicture`` 少个括号（题库里真的有）；
    2. 截断在语句中间 → ``\\draw (A)--(B`` 半句；
    3. 只写了 ``\\end{tikzpicture}`` / 只写了绘图命令，没有 begin；
    4. 用 ``$$ ... $$``、``\\[ ... \\]`` 把代码包起来；
    5. ``\\begin{tikzpicture}[scale=0.75`` 选项括号没关，或最后一句忘了分号。

    补不出来（比如压根不是 TikZ）就照旧抛 ``TikzError``。
    """
    fixes: list[str] = []
    text = _FENCE.sub("", (code or "").strip())
    text = _DOLLAR_WRAP.sub("", text).strip()
    if not text:
        raise TikzError("代码为空")
    original = text

    begin = "\\begin{tikzpicture}"
    end = "\\end{tikzpicture}"
    block = _TIKZ_BLOCK.search(text)
    if block:
        chunk = block.group(0)
        body = chunk[len(begin) : chunk.rfind(end)]
    elif begin in text:
        body = text[text.find(begin) + len(begin) :]
        fixes.append("补了 \\end{tikzpicture}")
    else:
        # 残缺的 \begin{tikzpictur 也算开头
        m = re.search(r"\\begin\s*\{?\s*tikzpictur[a-z]*\}?", text, re.I)
        if m and text.lstrip().startswith("\\begin"):
            body = text[m.end() :]
            fixes.append("补了 \\begin/\\end{tikzpicture}")
        elif text.lstrip().startswith("\\"):
            # 允许只写绘图命令，自动补环境
            body = text
            fixes.append("自动补了 tikzpicture 环境")
        else:
            raise TikzError("不是 TikZ 代码")

    body, closed = _close_options(body)
    if closed:
        fixes.append("补了选项括号 ]")
    body = _END_TAIL.sub("", body)  # 丢掉残缺的 \end{tikzpictur…
    if _CJK.search(body):
        # 汉字在 TeX 里是硬错误，不删就整张图作废；删掉图还在，只是缺中文标注
        body = _CJK.sub("", body)
        fixes.append("去掉了图里的汉字（TeX 引擎不支持中文）")
    body, action = _trim_to_statement(body)
    if action == "trimmed":
        fixes.append("切掉了没写完的最后一句")
    elif action == "semicolon":
        fixes.append("补了句末分号")

    raw_body = body
    body = body.strip()
    # 选项本来就紧跟在 \begin{tikzpicture} 后面是常态，那不算"修"；
    # 只有被折行挤到下一行的才需要拉回来（否则每张图都报一句假修复）
    moved = False
    if body.startswith("[") and "[" in raw_body:
        moved = any(ch in raw_body[: raw_body.find("[")] for ch in "\r\n")
    reflowed = False
    if body.startswith("["):
        # 选项一律贴着 \begin{tikzpicture} 输出；只有"原本被折行挤下去"才算修过
        cut = body.find("]")
        opts, rest = body[: cut + 1], body[cut + 1 :].strip()
        text = begin + opts + ("\n" + rest + "\n" if rest else "") + end
        if moved:
            reflowed = True
            fixes.append("环境选项拉回到 \\begin 后面")
    else:
        text = begin + ("\n" + body + "\n" if body else "") + end

    if len(text) > MAX_CODE:
        raise TikzError(f"代码过长（{len(text)} 字符）")

    hit = _BANNED.search(text)
    if hit:
        raise TikzError(f"含不允许的命令 {hit.group(0)}")

    # 只是空行/缩进被我重排过、内容其实一样：保留用户原来的排版，别白改一遍
    if not reflowed and _squash(text) == _squash(original):
        return original.strip(), []
    return text.strip(), fixes


def _squash(text: str) -> str:
    """去掉所有空白，只比"内容"——用来判断修复是否真的改了东西。"""
    return re.sub(r"\s+", "", text or "")


def normalise(code: str) -> str:
    """把模型给的东西收成一段纯 tikzpicture（编译前最后一道闸）。"""
    return repair_environment(code)[0]


def needs_pgfplots(code: str) -> bool:
    return "\\begin{axis}" in code or "\\addplot" in code


class _Engine:
    """node 渲染进程的封装：懒启动、串行、超时即杀。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._seq = 0

    # -- 进程管理 ---------------------------------------------------------
    def _node(self) -> str:
        exe = shutil.which("node")
        if exe:
            return exe
        # 启动器可能不在 PATH 里（比如从双击 .vbs 起的进程）
        for guess in (ROOT / "runtime" / "node.exe", Path.home() / ".workbuddy" / "binaries" / "node" / "node.exe"):
            if guess.exists():
                return str(guess)
        raise TikzError("找不到 node，无法渲染题图")

    def _start(self) -> None:
        if not SERVER.exists():
            raise TikzError("缺少 tools/tikz_server.cjs")
        env = dict(os.environ, NODE_NO_WARNINGS="1")
        kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "bufsize": 1,
            "cwd": str(ROOT),
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW：无窗口启动时别弹黑框
        self._proc = subprocess.Popen([self._node(), str(SERVER)], **kwargs)
        ready = self._readline(30.0)
        if not ready or not ready.strip().startswith("READY"):
            self._kill()
            raise TikzError("TikZ 渲染服务启动失败")

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def _readline(self, timeout: float) -> str | None:
        """带超时的 readline：管道没数据时 readline 会一直阻塞。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        box: dict[str, str | None] = {}

        def run() -> None:
            try:
                box["line"] = proc.stdout.readline()
            except (OSError, ValueError):
                box["line"] = None

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TikzError(f"渲染超时（{timeout:.0f}s）")
        return box.get("line")

    # -- 渲染 -------------------------------------------------------------
    def render(self, body: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        payload = {"body": body, "withPlots": needs_pgfplots(body)}
        with self._lock:
            for attempt in range(2):
                try:
                    if self._proc is None or self._proc.poll() is not None:
                        self._start()
                    self._seq += 1
                    assert self._proc is not None and self._proc.stdin is not None
                    self._proc.stdin.write(json.dumps({**payload, "id": self._seq}, ensure_ascii=False) + "\n")
                    self._proc.stdin.flush()
                    line = self._readline(timeout)
                    if not line:
                        raise TikzError("渲染服务无响应")
                    msg = json.loads(line)
                    if not msg.get("ok"):
                        raise TikzError(str(msg.get("error") or "编译失败"))
                    svg = str(msg.get("svg") or "")
                    if "<svg" not in svg.lower():
                        raise TikzError("没有产出 SVG")
                    return svg
                except (BrokenPipeError, json.JSONDecodeError, OSError):
                    self._kill()
                except TikzError:
                    # 超时或引擎崩：进程状态不可信，直接杀掉重来
                    if self._proc is not None and self._proc.poll() is not None:
                        self._kill()
                    raise
            raise TikzError("渲染服务反复失败")


_engine = _Engine()


# 输出端后处理的版本号。改了下面对 SVG 的修补就要 +1，否则老缓存图会继续被使用。
POST_VER = "1"

_PATH_TAG = re.compile(r"<path\b[^>]*/>")
_D_ATTR = re.compile(r'\bd="([^"]*)"')
_DASH_ATTR = re.compile(r'\bstroke-dasharray="?([^"\s>]+)"?')
_FILL_ATTR = re.compile(r'\bfill="([^"]*)"')
_D_TOKEN = re.compile(r"[MmLlHhVvZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _polylines(d: str) -> list[list[tuple[float, float]]] | None:
    """把只含直线命令的 path 拆成折线组；遇到曲线/弧就返回 None（不动它）。"""
    # 先确认整串只有 M/L/H/V/Z：否则 Q/C/S/A/T 会被分词器悄悄丢掉，
    # 后面按折线重建就把曲线画成了直线 —— 宁可不处理，也不能画错。
    bare = re.sub(r"[MmLlHhVvZz]", "", d or "")
    if re.search(r"[A-Za-z]", bare):
        return None
    tokens = _D_TOKEN.findall(d or "")
    if not tokens:
        return None
    out: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = 0.0
    start = (0.0, 0.0)
    cmd = "M"
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha():
            cmd = tok
            i += 1
            if cmd in "Zz":
                if cur:
                    if cur[0] != cur[-1]:
                        cur.append(cur[0])
                    out.append(cur)
                    cur = []
                x, y = start
                continue
            continue
        if cmd not in "MmLlHhVv":
            return None
        try:
            nums = [float(tokens[i])]
            need = 1 if cmd in "HhVv" else 2
            if need == 2:
                nums.append(float(tokens[i + 1]))
                i += 2
            else:
                i += 1
        except (IndexError, ValueError):
            return None
        a, b = nums[0], (nums[1] if len(nums) > 1 else 0.0)
        if cmd == "M":
            if cur:
                out.append(cur)
            x, y = a, b
            start = (x, y)
            cur = [(x, y)]
            cmd = "L"  # M 之后的隐式坐标按 L 处理
        elif cmd == "m":
            if cur:
                out.append(cur)
            x, y = x + a, y + b
            start = (x, y)
            cur = [(x, y)]
            cmd = "l"
        elif cmd == "L":
            x, y = a, b
            cur.append((x, y))
        elif cmd == "l":
            x, y = x + a, y + b
            cur.append((x, y))
        elif cmd == "H":
            x = a
            cur.append((x, y))
        elif cmd == "h":
            x += a
            cur.append((x, y))
        elif cmd == "V":
            y = a
            cur.append((x, y))
        elif cmd == "v":
            y += a
            cur.append((x, y))
        else:
            return None
    if cur:
        out.append(cur)
    return out


def _seg_key(a: tuple[float, float], b: tuple[float, float]) -> tuple:
    """线段归一化（方向无关），用于判断"是不是同一条线"。"""
    pa = (round(a[0], 2), round(a[1], 2))
    pb = (round(b[0], 2), round(b[1], 2))
    return (pa, pb) if pa <= pb else (pb, pa)


def dashed_over_solid(svg: str) -> str:
    """让虚线压过与它重合的实线 —— 修「立体图上全是实线」。

    模型画长方体/棱锥最常见的写法是:

        \\draw (A)--(B)--(C)--(D)--cycle;   % 底面一圈实线
        \\draw[dashed] (A)--(D);            % 再补"被挡住"的那条

    两条线**完全重合**，虚线画在实线上面什么也看不出来 —— 图上一根虚线都没有，
    学生根本分不清哪些棱在后头。TeX 本身也是这个结果，所以只能在这里补救：
    把与虚线重合的实线整段删掉，让虚线露出来。
    """
    if "<path" not in svg:
        return svg
    tags = list(_PATH_TAG.finditer(svg))
    if not tags:
        return svg
    parsed: list[tuple[re.Match[str], list[list[tuple[float, float]]], bool]] = []
    dashed_keys: set[tuple] = set()
    for m in tags:
        tag = m.group(0)
        dm = _D_ATTR.search(tag)
        if not dm:
            continue
        fill = _FILL_ATTR.search(tag)
        if fill and fill.group(1).strip().lower() not in ("none", ""):
            continue  # 填充图形不参与（遮住的到底是谁说不清）
        lines = _polylines(dm.group(1))
        if not lines:
            continue
        is_dashed = bool(_DASH_ATTR.search(tag))
        parsed.append((m, lines, is_dashed))
        if is_dashed:
            for pts in lines:
                for a, b in zip(pts, pts[1:]):
                    if a != b:
                        dashed_keys.add(_seg_key(a, b))
    if not dashed_keys:
        return svg
    out = svg
    for m, lines, is_dashed in reversed(parsed):
        if is_dashed:
            continue
        kept: list[list[tuple[float, float]]] = []
        changed = False
        for pts in lines:
            run: list[tuple[float, float]] = []
            for a, b in zip(pts, pts[1:]):
                if a != b and _seg_key(a, b) in dashed_keys:
                    changed = True
                    if len(run) >= 2:
                        kept.append(run)
                    run = []
                else:
                    if not run:
                        run = [a]
                    run.append(b)
            if len(run) >= 2:
                kept.append(run)
        if not changed:
            continue
        tag = m.group(0)
        if not kept:
            out = out.replace(tag, "", 1)
            continue
        d = "".join(
            "M%.3f %.3f%s" % (pts[0][0], pts[0][1], "".join("L%.3f %.3f" % p for p in pts[1:]))
            for pts in kept
        )
        out = out.replace(tag, _D_ATTR.sub(f'd="{d}"', tag, count=1), 1)
    return out


def render(tikz_code: str, *, use_cache: bool = True) -> RenderResult:
    """把 TikZ 代码编译成 SVG。失败抛 :class:`TikzError`。

    ``RenderResult.code`` 是**修好之后**的代码，调用方应该回写数据库——
    否则坏代码一直躺在题目里，每次渲染都要重新补一遍，编辑器里也看不出被修过。
    """
    ensure_fonts()
    body, fixes = repair_environment(tikz_code)
    key = hashlib.sha1(f"{body}|{POST_VER}".encode("utf-8")).hexdigest()[:20]
    cache = RENDER_DIR / f"{key}.svg"
    if use_cache and cache.exists():
        try:
            svg = cache.read_text(encoding="utf-8")
            if "<svg" in svg.lower():
                return RenderResult(svg=svg, cached=True, ms=0, code=body, fixes=fixes)
        except OSError:
            pass

    started = time.monotonic()
    svg = dashed_over_solid(_engine.render(body))
    ms = int((time.monotonic() - started) * 1000)

    try:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(svg, encoding="utf-8")
    except OSError:
        pass  # 缓存失败不影响渲染结果
    return RenderResult(svg=svg, cached=False, ms=ms, code=body, fixes=fixes)


def shutdown() -> None:
    """退出时收掉 node 子进程，避免留孤儿进程。"""
    _engine._kill()


atexit.register(shutdown)
