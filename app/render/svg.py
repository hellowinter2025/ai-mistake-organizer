"""图形处理：AI 生成 SVG 的安全过滤与栅格化。

安全前提：SVG 是模型输出的，属于不可信内容，直接塞进页面等于给 XSS 开门。
这里按白名单重建 SVG，只保留绘图形元素与几何属性，脚本/外链/样式一律丢弃。
"""
from __future__ import annotations

import base64
import hashlib
import re
import subprocess
from pathlib import Path

from lxml import etree

from ..config import CACHE_DIR

SVG_NS = "http://www.w3.org/2000/svg"
ALLOWED_TAGS = {
    "svg", "g", "path", "line", "rect", "circle", "ellipse", "polygon", "polyline",
    "text", "tspan", "title", "desc", "defs", "marker",
}
# 只允许几何/排版相关属性；任何 on* / href / style 都被排除
ALLOWED_ATTRS = {
    "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "d", "points", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-opacity", "opacity",
    "transform", "font-size", "font-family", "font-weight", "font-style", "text-anchor",
    "dominant-baseline", "dx", "dy", "id", "class", "marker-end", "marker-start", "refX", "refY",
    "markerWidth", "markerHeight", "orient", "preserveAspectRatio", "xmlns",
}
DROP_TAGS = {"script", "style", "image", "use", "foreignObject", "a", "animate", "animateTransform",
             "set", "iframe", "video", "audio", "object", "embed", "link", "meta"}

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
TIKZ_FONT_CSS = WEB_DIR / "vendor" / "tikz" / "fonts.css"
TIKZ_FONT_DIR = WEB_DIR / "vendor" / "tikz" / "bakoma" / "ttf"

_FONT_FAMILY_RE = re.compile(r'font-family="([A-Za-z][A-Za-z0-9]*)"')


def svg_ratio(svg: str) -> float:
    """从 viewBox 取宽高比（宽/高）。取不到就当方图。

    用来判断"该图是不是宽扁的一条"——横排工艺流程图、宽幅函数图属于这种，
    塞进半栏或两列表格里会缩成看不清的小带子。
    """
    match = re.search(
        r'viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', svg or ""
    )
    if not match:
        return 1.0
    try:
        w, h = float(match.group(1)), float(match.group(2))
    except ValueError:
        return 1.0
    return w / h if h > 0 else 1.0


def figure_is_wide(question: dict, svg: str | None = None) -> bool:
    """这张题图该不该整行铺开（PDF 的 ``figure-wide`` / DOCX 的 16.2cm）。

    宽高比 ≥2.4 的一定是（横排函数图、单行流程图）。但流程图**折行**之后
    会变成"两三条并排"，宽高比掉到 1.5~2.2 —— 按 2.4 判会把它塞回半栏，
    整张图缩成看不清的一小块，所以给流程图单独放宽到 1.5。
    """
    code = svg if svg is not None else str(question.get("figure_svg") or "")
    if not code:
        return False
    # 流程图（含有机合成路线）一律整行：横排一条是它的自然形态，塞进右上角
    # 34% 的浮动小框里必然缩成看不清的细条。折行后宽高比会掉到 2 以下，
    # 不能拿比例当唯一判据 —— 只要源字段是流程图就整行。
    if question.get("figure_flow"):
        return True
    return svg_ratio(code) >= 2.4


def font_css_for(svgs: list[str]) -> str:
    """把题图里实际用到的 TeX 字体内嵌成 ``@font-face``（base64）。

    TikZ 出的 SVG 里文字是 ``font-family="cmsy7"`` 这种 TeX 字体名，字符码位也是
    TeX 编码（``\\prime`` 正好落在 ASCII '0' 的位置）。浏览器拿不到这个字体时会
    用默认字体按 ASCII 渲染 —— 于是 A' 变成 A⁰、下标全乱。

    DOCX 那条链（``svg_to_png``）是外链 fonts.css 渲染的，没问题；PDF 是直接
    把 SVG 内联进 HTML 再打印，早先没带字体，就出现了"顶点标成 A 的 0 次方"。
    这里只挑当前文档真正引用的家族内嵌（每个 30KB 上下），不整包塞 140 个。
    """
    families: list[str] = []
    for svg in svgs:
        for name in _FONT_FAMILY_RE.findall(svg or ""):
            if name not in families:
                families.append(name)
    rules: list[str] = []
    for name in families:
        path = TIKZ_FONT_DIR / f"{name}.ttf"
        if not path.exists():
            continue
        b64 = base64.b64encode(path.read_bytes()).decode()
        rules.append(
            "@font-face{font-family:%s;src:url(data:font/ttf;base64,%s) format('truetype');}"
            % (name, b64)
        )
    return "\n".join(rules)

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


# style 里只放这些排版属性；出现过 url()/expression 之类的值整条丢掉
SAFE_STYLE_PROPS = {
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-opacity", "opacity",
    "font-size", "font-family", "font-weight", "font-style", "text-anchor", "letter-spacing",
    "dominant-baseline", "paint-order",
}
_STYLE_DANGER = re.compile(r"url\s*\(|expression|javascript:|@import|behaviou?r\s*:", re.I)


def _clean_style(value: str) -> str:
    """把 ``style=""`` 拆开逐条白名单过滤。

    RDKit 出的 SVG 把 ``stroke`` 全放在 style 里（``<path style='fill:none;
    stroke:#000000;stroke-width:2.0px'>``），整条属性一删，化学键就全都不见了
    —— 卷面上只剩 OH、O 几个字母。所以不能一刀切掉，只能逐条筛。
    """
    kept: list[str] = []
    for decl in value.split(";"):
        if ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip().lower()
        val = val.strip()
        if prop not in SAFE_STYLE_PROPS or not val or _STYLE_DANGER.search(val):
            continue
        kept.append(f"{prop}:{val}")
    return ";".join(kept)


def _clean_attrs(el) -> None:
    for key in list(el.attrib):
        local = key.split("}")[-1]
        if local == "style":
            cleaned = _clean_style(el.attrib[key])
            if cleaned:
                el.attrib[key] = cleaned
            else:
                del el.attrib[key]
            continue
        if local.startswith("on") or local.lower() in ("href", "xlink:href", "src"):
            del el.attrib[key]
            continue
        if local not in ALLOWED_ATTRS:
            del el.attrib[key]
            continue
        if local == "fill" and re.search(r"url\(", el.attrib[key]):
            del el.attrib[key]
        if local == "class":
            del el.attrib[key]


def sanitize_svg(raw: str) -> str | None:
    """按白名单重建 SVG；非法或空内容返回 None。"""
    if not raw or "<svg" not in raw.lower():
        return None
    text = raw.strip()
    text = re.sub(r"<\?xml[^>]*\?>", "", text)
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.I)
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)
        root = etree.fromstring(text.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        return None
    if root is None or etree.QName(root).localname != "svg":
        # 有些模型会多包一层 <g>，尝试找内部的 svg
        found = root.find(".//{http://www.w3.org/2000/svg}svg") if root is not None else None
        if found is None:
            return None
        root = found

    svg_size_hint = {"w": root.attrib.get("width", "400"), "h": root.attrib.get("height", "300")}

    def walk(node) -> None:
        for child in list(node):
            # 注释 / 处理指令的 tag 是 lxml 的内建函数而不是字符串，
            # 直接 QName(child) 会抛 ValueError（RDKit 出的 SVG 就带注释）
            if not isinstance(child.tag, str):
                node.remove(child)
                continue
            local = etree.QName(child).localname
            if local in DROP_TAGS:
                node.remove(child)
                continue
            if local not in ALLOWED_TAGS:
                # 未知容器：保留内容、去掉标签
                walk(child)
                node.remove(child)
                continue
            _clean_attrs(child)
            walk(child)

    _clean_attrs(root)
    walk(root)

    # 统一命名空间与尺寸（不做重新打标签，避免 lxml 生成 ns0: 前缀）
    root.attrib.pop("width", None)
    root.attrib.pop("height", None)
    if "viewBox" not in root.attrib:
        w = re.sub(r"[^\d.]", "", str(svg_size_hint.get("w", "400"))) or "400"
        h = re.sub(r"[^\d.]", "", str(svg_size_hint.get("h", "300"))) or "300"
        root.set("viewBox", f"0 0 {w} {h}")
    root.set("preserveAspectRatio", "xMidYMid meet")
    # 有命名空间时 lxml 序列化会自己带 xmlns，再 set 一次会写出两个（XML 非法）
    if etree.QName(root).namespace is None and "xmlns" not in root.attrib:
        root.set("xmlns", SVG_NS)
    try:
        etree.cleanup_namespaces(root)
    except (ValueError, TypeError):
        pass

    out = etree.tostring(root, encoding="unicode")
    if len(out) > 300_000:
        return None
    if not re.search(r"<(?:[\w.-]+:)?(path|line|rect|circle|ellipse|polygon|polyline)\b", out):
        return None
    return out


def is_dead_svg(svg: str) -> bool:
    """判断是不是"死图"——有线段却画不出来。

    早期版本的 ``sanitize_svg`` 一刀切删掉 ``style`` 属性，于是 RDKit 出的键线
    变成 ``<path d="M .. L .."/>``：SVG 规范里 ``stroke`` 默认 ``none``、``fill``
    填不了零面积的线段，结果整张结构式只剩 O、OH 几个字母飘着。这类 SVG 一旦
    存进库就会一直错下去，所以识别出来好让上层用 ``figure_smiles`` 重画。

    判据：有 ``<path>`` 却没有任何 stroke 声明（属性或 style 里都没有）。
    """
    if not svg or "<path" not in svg:
        return False
    return not re.search(r"stroke(?:-width|-opacity)?\s*[:=]", svg)


def revive_figure_svg(question: dict) -> str:
    """拿到能用的题图 SVG：**以源字段（smiles/flow）为准现场编译**，存量只兜底。

    以前是"figure_svg 健康就直接用"，结果排版算法升级后库里那张旧图（L 形
    折行、条件写「加热」）一直原样显示 —— 预览和导出看着像没重新渲染。
    编译本身有内容哈希缓存，重复取用只是算一次 sha1，所以一律现编：

    - 有 ``figure_flow`` / ``figure_smiles`` → 现场编译（最新排版）
    - 没有源字段 → 用 ``figure_svg``（死图也原样返回，调用方自己兜底）
    - 编译失败 → 退回 ``figure_svg``，不让导出崩掉

    ``figure_tikz`` 不走这里重编（要起 node 进程，太贵），交给 ``_resolve_figure``。
    """
    svg = str(question.get("figure_svg") or "")
    smiles = question.get("figure_smiles")
    flow = question.get("figure_flow")

    rebuilt = ""
    try:
        if flow:
            from . import flow as flow_mod

            rebuilt = sanitize_svg(flow_mod.render(flow, use_cache=True)) or ""
        elif smiles:
            from . import chem

            mapping = chem.normalise(smiles)
            if mapping:
                rebuilt = sanitize_svg(chem.render_svg(mapping, use_cache=True)) or ""
    except Exception:  # noqa: BLE001 - 重画失败不该让导出整个崩掉
        rebuilt = ""
    return rebuilt or svg


def svg_size(svg: str) -> tuple[int, int]:
    m = re.search(r'viewBox="([-\d.\s]+)"', svg)
    if m:
        parts = re.split(r"[\s,]+", m.group(1).strip())
        if len(parts) == 4:
            try:
                w, h = float(parts[2]), float(parts[3])
                if w > 0 and h > 0:
                    return int(round(w)), int(round(h))
            except ValueError:
                pass
    return 400, 300


def png_url_for(svg: str, width_px: int = 1200, scale: int = 2) -> tuple[str, int, int] | None:
    """把 SVG 栅格化成 PNG，返回 (访问 URL, 像素宽, 像素高)。

    **全站题图的唯一渲染出口**：编辑器预览、题库卡片、PDF、DOCX 都拿这里的
    PNG，所见即所得。以前预览在浏览器里插 SVG（DOMPurify），和导出走两条路，
    化学键被浏览器清洗掉都发现不了。
    """
    path = svg_to_png(svg, width_px=width_px, scale=scale)
    if path is None:
        return None
    vw, vh = svg_size(sanitize_svg(svg) or svg)
    px_h = max(1, round(width_px * vh / max(1, vw))) * scale
    return f"/api/figures/img/{path.stem}", width_px * scale, px_h


def svg_to_png(svg: str, width_px: int = 760, scale: int = 2) -> Path | None:
    """把 SVG 栅格化成 PNG（Word 不支持内联 SVG，只能走图片）。"""
    safe = sanitize_svg(svg)
    if not safe:
        return None
    key = hashlib.sha1(f"{safe}|{width_px}|{scale}".encode()).hexdigest()[:20]
    out = CACHE_DIR / f"fig_{key}.png"
    if out.exists() and out.stat().st_size > 1200:
        return out
    browser = next((p for p in BROWSERS if Path(p).exists()), None)
    if browser is None:
        return None

    vw, vh = svg_size(safe)
    height = max(60, int(round(width_px * vh / max(1, vw))))
    # TikZ 出的 SVG 里文字用 TeX 字体，栅格化时也要把字体带上，
    # 否则 Word 里的题图会退化成系统衬线体、字符间距全变。
    fonts = ""
    if TIKZ_FONT_CSS.exists():
        fonts = f"<link rel='stylesheet' href='{TIKZ_FONT_CSS.as_uri()}'>"
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>%s<style>"
        "html,body{margin:0;padding:0;background:#fff;}"
        "svg{display:block;width:%dpx;height:%dpx;}"
        "</style></head><body>%s</body></html>" % (fonts, width_px, height, safe)
    )
    wrapper = CACHE_DIR / f"fig_{key}.html"
    wrapper.write_text(html, encoding="utf-8")
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=%d" % scale,
        "--default-background-color=FFFFFFFF",
        "--window-size=%d,%d" % (width_px, height),
        "--screenshot=%s" % out,
        wrapper.as_uri(),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        wrapper.unlink(missing_ok=True)
    if out.exists() and out.stat().st_size > 1200:
        return out
    return None
