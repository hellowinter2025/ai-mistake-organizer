"""化学结构式渲染：SMILES → SVG / PNG。

为什么不让模型直接画 TikZ：
- SMILES 有严格语法，``Chem.MolFromSmiles`` 返回 ``None`` 就是错的，可以程序化判定；
- 还能反推分子式，和题干给的分子式核对一遍，做语义校验；
- TikZ 由模型手算坐标，画歪了照样编译通过，错不自知。

RDKit 是**可选依赖**：没装就整体禁用（:func:`available` 为 False），
上层自动退回「贴原题截图」，不影响主流程。
"""
from __future__ import annotations

import hashlib
import io as _io
import json
import re
from typing import Any

from ..config import CACHE_DIR

CACHE = CACHE_DIR / "chem"

MAX_MOLECULES = 6
TILE_W, TILE_H = 340, 240  # 单分子画布（SVG 用户单位，约等于 pt）
GAP = 10

# 模型偶尔会把整段话塞进来，先拦明显不是 SMILES 的
_SMILES_OK = re.compile(r"^[A-Za-z0-9@+\-\[\]\(\)=#$:/\\.%]+$")

try:  # pragma: no cover - 取决于环境是否装了 rdkit
    from rdkit import Chem, RDLogger
    from rdkit.Chem import rdMolDescriptors
    from rdkit.Chem.Draw import rdMolDraw2D

    RDLogger.DisableLog("rdApp.*")  # 解析失败会往 stderr 刷日志，我们只要返回值
    _HAS_RDKIT = True
except Exception:  # noqa: BLE001 - 缺依赖/二进制不匹配都按“没装”处理
    _HAS_RDKIT = False


class ChemError(RuntimeError):
    """结构式渲染失败：上层据此退回「贴原题截图」。"""


def available() -> bool:
    """本机是否具备渲染能力（装了 RDKit 且能 import）。"""
    return _HAS_RDKIT


def _mol(smiles: str) -> Any:
    if not _HAS_RDKIT:
        raise ChemError("未安装 RDKit，无法渲染化学结构式")
    text = (smiles or "").strip()
    if not text or not _SMILES_OK.match(text):
        return None
    try:
        return Chem.MolFromSmiles(text)
    except Exception:  # noqa: BLE001 - 第三方解析异常类型不稳定
        return None


def is_valid(smiles: str) -> bool:
    """这条 SMILES 能不能解析（供流程图等上层做校验，不抛异常）。"""
    if not _HAS_RDKIT:
        return False
    return _mol(smiles) is not None


def draw_tile(smiles: str, w: int = TILE_W, h: int = TILE_H, line_width: float = 2.0) -> str:
    """画单个分子，返回**内部** SVG 片段（去掉外层 <svg> 与白底）。

    给流程图内嵌用：调用方自己决定摆在哪个 translate 里，所以这里不套画布。
    流程图里的结构式缩得比独立结构式小，线宽要相应调细，否则比流程框还重。
    """
    return _strip_svg(str(_draw_one(smiles, png=False, w=w, h=h, line_width=line_width)))


def formula(smiles: str) -> str:
    """由 SMILES 反推分子式（如 C6H6O），解析失败返回空串。"""
    mol = _mol(smiles)
    if mol is None:
        return ""
    try:
        return rdMolDescriptors.CalcMolFormula(mol)
    except Exception:  # noqa: BLE001
        return ""


def normalise(raw: Any, rejects: list[tuple[str, str]] | None = None) -> dict[str, str]:
    """把模型给的 smiles 收成 ``{名称: SMILES}``，丢掉解析不了的。

    模型可能给 dict、list、裸字符串，也可能给**序列化后的 JSON 文本**
    （``figure_smiles`` 在库里就是 TEXT，编辑器把它原样回传）——这几种都要认。
    还会把教材写法 ``CH3CH2OH`` 当成 SMILES（那不是合法 SMILES），这类直接剔掉；
    剔掉的原因通过 ``rejects`` 回传，好让上层提示到底是哪一个写错了。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if text[:1] in "{[":
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, (dict, list)):
                raw = decoded

    items: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, (list, tuple)):
                for one in value:
                    items.append((str(key), str(one)))
            else:
                items.append((str(key), str(value)))
    elif isinstance(raw, (list, tuple)):
        for one in raw:
            if isinstance(one, dict):
                items.extend((str(k), str(v)) for k, v in one.items())
            else:
                items.append(("", str(one)))
    elif raw:
        items.append(("", str(raw)))

    out: dict[str, str] = {}
    for name, value in items:
        # 模型常写成 "苯酚: Oc1ccccc1" 一行一个
        if ":" in value and not _SMILES_OK.match(value.split(":", 1)[0].strip()):
            value = value.split(":", 1)[1]
        smi = value.strip().strip("`'\" ")
        smi = smi.replace(" ", "")
        if not smi or _mol(smi) is None:
            if rejects is not None:
                rejects.append(((name or "").strip() or "未命名结构式", smi or str(value).strip()))
            continue
        label = (name or "").strip().strip("`'\" ")
        if label in out:
            continue
        out[label or f"结构式{len(out) + 1}"] = smi
        if len(out) >= MAX_MOLECULES:
            break
    return out


def _draw_one(smiles: str, *, png: bool, w: int, h: int, line_width: float = 2.0) -> str | bytes:
    mol = _mol(smiles)
    if mol is None:
        raise ChemError(f"无法解析的 SMILES：{smiles[:40]}")
    drawer = rdMolDraw2D.MolDraw2DCairo(w, h) if png else rdMolDraw2D.MolDraw2DSVG(w, h)
    opt = drawer.drawOptions()
    opt.bondLineWidth = line_width
    opt.padding = 0.08          # 四周留白，别顶到画布边
    opt.useBWAtomPalette()      # 单色：试卷/Word 里不出现彩色杂原子
    # 必须显式清背景：Cairo 的缓冲区不清就是全黑；SVG 那边多出的白底矩形
    # 由 _strip_svg 去掉，不影响并排拼接。
    opt.clearBackground = True
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    data = drawer.GetDrawingText()
    return data.encode("utf-8") if (png and isinstance(data, str)) else data


def _strip_svg(svg: str) -> str:
    """去掉 XML 声明、外层 <svg> 包装和白色背景矩形，只留图形内容。"""
    body = re.sub(r"^.*?<svg[^>]*>", "", svg, count=1, flags=re.S)
    body = re.sub(r"</svg>\s*$", "", body)
    body = re.sub(r"<rect[^>]*>\s*</rect>", "", body)
    body = re.sub(r"<rect[^>]*/>", "", body)
    return body.strip()


# 渲染画法版本：TILE 尺寸/线宽/配色有变动就 +1，让旧缓存自动失效
LAYOUT_VER = "1"


def _cache_path(mapping: dict[str, str], suffix: str) -> Any:
    key = hashlib.sha1(
        (LAYOUT_VER + "|" + "|".join(f"{k}={v}" for k, v in mapping.items())).encode("utf-8")
    ).hexdigest()[:20]
    return CACHE / f"{key}.{suffix}"


def render_svg(mapping: dict[str, str], *, use_cache: bool = True) -> str:
    """多个分子横向拼成一张 SVG（矢量，供网页预览与 PDF 打印）。"""
    if not mapping:
        raise ChemError("没有可渲染的 SMILES")
    cache = _cache_path(mapping, "svg")
    if use_cache and cache.exists():
        try:
            text = cache.read_text(encoding="utf-8")
            if "<svg" in text.lower():
                return text
        except OSError:
            pass

    tiles = [_strip_svg(str(_draw_one(smi, png=False, w=TILE_W, h=TILE_H))) for smi in mapping.values()]
    total = TILE_W * len(tiles) + GAP * (len(tiles) - 1)
    parts = [
        f"<g transform='translate({i * (TILE_W + GAP)},0)'>{tile}</g>" for i, tile in enumerate(tiles)
    ]
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' version='1.1' "
        f"width='{total}' height='{TILE_H}' viewBox='0 0 {total} {TILE_H}'>{''.join(parts)}</svg>"
    )
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(svg, encoding="utf-8")
    except OSError:
        pass
    return svg


def render_png(mapping: dict[str, str], *, width_px: int = 1200, use_cache: bool = True) -> bytes:
    """多个分子横向拼成一张 PNG（供 DOCX 插入：Word 对 SVG 支持有限）。"""
    if not mapping:
        raise ChemError("没有可渲染的 SMILES")
    cache = _cache_path(mapping, f"{width_px}.png")
    if use_cache and cache.exists():
        try:
            data = cache.read_bytes()
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
        except OSError:
            pass

    from PIL import Image  # 项目本来就有 Pillow

    count = len(mapping)
    tile_w = max(160, (width_px - GAP * (count - 1)) // count)
    tile_h = int(tile_w * TILE_H / TILE_W)
    tiles: list[Image.Image] = []
    for smi in mapping.values():
        raw = _draw_one(smi, png=True, w=tile_w, h=tile_h)
        tiles.append(Image.open(_io.BytesIO(raw)).convert("RGB"))

    canvas = Image.new("RGB", (tile_w * count + GAP * (count - 1), tile_h), "white")
    for i, tile in enumerate(tiles):
        canvas.paste(tile, (i * (tile_w + GAP), 0))
    buf = _io.BytesIO()
    canvas.save(buf, format="PNG", dpi=(300, 300))
    data = buf.getvalue()
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    except OSError:
        pass
    return data
