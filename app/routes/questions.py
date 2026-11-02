"""题库 CRUD。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from PIL import Image
from starlette.concurrency import run_in_threadpool

from .. import db, imaging
from ..ai import parser as ai_parser
from ..config import CROP_DIR
from . import common

router = APIRouter(prefix="/api/questions", tags=["questions"])

_MISSING = object()

TEXT_FIELDS = ("stem", "answer", "error_reason", "solution_fix", "solution_alt", "notes")


def _scrub_text(data: dict[str, Any]) -> None:
    """保存前砍掉正文末尾混进来的 JSON 字段（``…。","knowledge_points":[…]``）。

    解析层已经拦了一道，但已污染的内容可能还躺在编辑器里（人工改一半再保存），
    这里做最后一道保险：只认我们自己的字段名 + 冒号，正常正文不会被误伤。

    顺带把**字面量** ``\\n`` / ``\\r`` / ``\\t`` 还原成真换行 —— 手工粘贴 AI 原文
    或旧数据里残留的那两个字符，页面不认，还会把 KaTeX 带崩。
    """
    for key in TEXT_FIELDS:
        value = data.get(key)
        if isinstance(value, str) and value:
            cleaned, cut = ai_parser.strip_json_tail(value)
            cleaned = ai_parser.unescape_literals(cleaned)
            if cut or cleaned != value:
                data[key] = cleaned

# 增强算法版本号。参与题图文件名哈希：改了 pipeline 就自动换新文件，
# 前端/浏览器不会继续吃旧图，存量图也可以靠「重新增强」一键重算。
ENHANCE_VER = "4"

EDITABLE = {
    "subject",
    "question_type",
    "stem",
    "answer",
    "error_reason",
    "solution_fix",
    "solution_alt",
    "knowledge_points",
    "difficulty",
    "tags",
    "notes",
    "starred",
    "figure_svg",
    "figure_tikz",
    "figure_smiles",
    "figure_flow",
    "needs_figure",
    "figure_box",
}


def _smiles_map(raw: Any) -> dict[str, str]:
    """把 figure_smiles 的各种存法（dict / JSON 文本 / 裸 SMILES）统一成 dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"结构式": raw.strip()}
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            return {"结构式": parsed}
    return {}


def _honour_needs_figure(data: dict[str, Any], payload: dict[str, Any]) -> None:
    """用户勾没勾「改用原题截图」，以**用户为准**。

    ``_resolve_figure`` 一编译成功就把 needs_figure 改回 False（它眼里"能画出来
    就不用贴图"），于是编辑器里勾好的「改用原题截图」保存后又被顶掉 ——
    用户反馈 #85QCUV 装置图就是这样：勾了，导出还是用 AI 那张画歪的。
    但图**没画出来**时不能听用户的：那时唯一能看的只有原题截图。
    """
    if "needs_figure" not in payload:
        return
    if not data.get("figure_svg"):
        return  # 编译失败 → 只能贴截图，保持 _resolve_figure 的判断
    data["needs_figure"] = 1 if payload.get("needs_figure") else 0


def _resolve_figure(data: dict[str, Any]) -> None:
    """图形解析优先级：工艺流程图 → 化学结构式（SMILES）→ TikZ → 都不行就贴原题截图。

    放在保存时做（而不是只靠解析时那次），这样用户在编辑器里改完代码
    点保存，图会跟着更新，不用重新跑一遍 AI。
    """
    raw_flow = data.get("figure_flow")
    if raw_flow:
        from ..render import flow as flow_mod
        from ..render.svg import sanitize_svg

        try:
            svg = sanitize_svg(flow_mod.render(raw_flow))
            if not svg:
                raise flow_mod.FlowError("渲染结果未通过安全校验")
        except Exception:  # noqa: BLE001 - 渲染失败不能挡住保存
            data["figure_svg"] = None
            data["needs_figure"] = True
            return
        data["figure_svg"] = svg
        data["needs_figure"] = False
        return

    if data.get("figure_smiles"):
        from ..render import chem
        from ..render.svg import sanitize_svg

        mapping = chem.normalise(_smiles_map(data["figure_smiles"]))
        if not mapping:
            data["figure_smiles"] = None
            data["figure_svg"] = None
            data["needs_figure"] = True
            return
        data["figure_smiles"] = mapping  # 回写清洗后的，前端看到的与画出来的一致
        try:
            svg = sanitize_svg(chem.render_svg(mapping))
            if not svg:
                raise chem.ChemError("编译结果未通过安全校验")
        except Exception:  # noqa: BLE001 - 渲染失败不能挡住保存
            data["figure_svg"] = None
            data["needs_figure"] = True
            return
        data["figure_svg"] = svg
        data["needs_figure"] = False
        return

    code = str(data.get("figure_tikz") or "").strip()
    if not code:
        return
    from ..render import tikz as tikz_mod
    from ..render.svg import sanitize_svg

    try:
        result = tikz_mod.render(code)
        svg = sanitize_svg(result.svg)
        if not svg:
            raise tikz_mod.TikzError("编译结果未通过安全校验")
    except Exception:  # noqa: BLE001 - 编译失败不能挡住保存
        data["figure_svg"] = None
        data["needs_figure"] = True
        return
    # 回写补全后的代码：模型给的 \end{tikzpicture 经常被截断，
    # 不写回去的话库里永远留着坏代码，每次渲染都要重补一遍。
    if result.code and result.code != code:
        data["figure_tikz"] = result.code
    data["figure_svg"] = svg
    data["needs_figure"] = False


def _drop_figure_file(raw: Any) -> None:
    """删掉不再使用的题图裁剪文件（只动 CROP_DIR 里的）。"""
    if not raw:
        return
    try:
        path = Path(str(raw)).resolve()
        if path.parent == CROP_DIR.resolve() and path.exists():
            path.unlink()
    except OSError:
        pass


def _render_figure_crop(q: dict[str, Any], box: Any) -> Path | None:
    """按归一化框 + 原始截图，重新生成增强后的题图（PNG），返回落盘路径。

    一律从 ``crop_path``（原始截图）重新算——增强是非线性链路（纸底归一会
    继续压白），拿上一版结果再跑一次只会越跑越糊。``ENHANCE_VER`` 参与
    token，改了算法就能自动换新文件名，绕开浏览器缓存。
    """
    crop_path = q.get("crop_path")
    if not box or not crop_path or not Path(str(crop_path)).exists():
        return None
    if isinstance(box, dict):
        box = [box.get(k) for k in ("x", "y", "w", "h")]
    try:
        x, y, w, h = [float(v) for v in list(box)[:4]]
    except (TypeError, ValueError):
        return None
    if not all(v == v for v in (x, y, w, h)):  # NaN
        return None

    x = min(max(x, 0.0), 0.999)
    y = min(max(y, 0.0), 0.999)
    w = min(max(w, 0.01), 1.0 - x)
    h = min(max(h, 0.01), 1.0 - y)

    try:
        with Image.open(str(crop_path)) as im:
            im = im.convert("RGB")
            width, height = im.size
            left = int(round(x * width))
            top = int(round(y * height))
            right = int(round((x + w) * width))
            bottom = int(round((y + h) * height))
            if right - left < 16 or bottom - top < 16:
                return None
            piece = im.crop((left, top, right, bottom))
    except (OSError, ValueError):
        return None

    # 框选即增强：去红蓝批注 → 去黑色手写 → 纸底归一 → 去噪 → 线稿化
    try:
        piece = imaging.enhance_figure(piece)
    except Exception:  # noqa: BLE001 - 增强失败不该挡住裁剪本身
        piece = piece.convert("L")

    seed = f"{q['uid']}:{left},{top},{right},{bottom}:{ENHANCE_VER}"
    token = hashlib.sha1(seed.encode()).hexdigest()[:20]
    target = CROP_DIR / f"{token}.png"
    if not target.exists():
        piece.save(target, "PNG", optimize=True)
    return target


def _apply_figure_box(uid: str, box: Any) -> None:
    """把归一化裁剪框（0~1，相对题目截图）落到真实图片上。

    这是「保留原图」的进阶用法：图形复杂、AI 画不出来，但整块题目截图里
    混着题干文字，导出时必须只贴图形本身。用户在前端框一下，这里负责
    裁好、增强（去批注、纸底归一）、存盘，导出时优先用这张图。
    """
    q = db.get_question_by_uid(uid)
    if not q:
        return
    old = q.get("figure_crop")

    if not box:
        if old:
            _drop_figure_file(old)
            db.update_question(int(q["id"]), {"figure_crop": None, "figure_box": None})
        return

    target = _render_figure_crop(q, box)
    if target is None:
        return
    if isinstance(box, dict):
        box = [box.get(k) for k in ("x", "y", "w", "h")]
    x, y, w, h = [round(float(v), 4) for v in list(box)[:4]]
    if old and old != str(target):
        _drop_figure_file(old)
    patch: dict[str, Any] = {
        "figure_crop": str(target),
        "figure_box": json.dumps([x, y, w, h]),
    }
    # 框选就是"我要用这张图"：顺手把 needs_figure 置上，否则导出还会拿 AI 重绘的图
    # 盖过去（实测就踩过）。只在框**变了**的时候置位，这样用户之后主动取消勾选
    # 再保存，选择能被保留下来。
    if _box_changed(q.get("figure_box"), [x, y, w, h]):
        patch["needs_figure"] = 1
    db.update_question(int(q["id"]), patch)


def _box_changed(prev: Any, current: list[float]) -> bool:
    if not prev:
        return True
    try:
        raw = json.loads(str(prev)) if not isinstance(prev, (list, tuple)) else list(prev)
    except (json.JSONDecodeError, TypeError):
        return True
    try:
        return [round(float(v), 4) for v in raw[:4]] != current
    except (TypeError, ValueError):
        return True


@router.get("")
async def list_questions(
    keyword: str = "",
    subject: str = "",
    starred: bool = False,
    limit: int = Query(60, ge=1, le=300),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    rows, total = db.list_questions(
        keyword=keyword.strip(), subject=subject.strip(), starred_only=starred, limit=limit, offset=offset
    )
    return {"items": [common.question_payload(q) for q in rows], "total": total}


def _raw_from_log(log_id: Any) -> str:
    """按调用日志号读回**完整**的模型原始响应。

    前端为了不把队列撑爆 localStorage，只拿到 1500 字的 ``raw_preview``；
    直接当 raw_ai 存会在 JSON 中途截断（flow 这类靠后的字段整段丢失）。
    完整响应在调用时已单独存档，这里按 log_id 取回来。
    """
    try:
        row = db.query_one("SELECT raw_path FROM ai_logs WHERE id = ?", (int(log_id),))
    except (TypeError, ValueError):
        return ""
    if not row or not row["raw_path"]:
        return ""
    from ..config import LOG_DIR

    path = (LOG_DIR.parent / str(row["raw_path"])).resolve()
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@router.post("")
async def create_question(payload: dict[str, Any]) -> dict[str, Any]:
    stem = str(payload.get("stem") or "").strip()
    if not stem:
        raise HTTPException(status_code=400, detail="题目内容不能为空")
    data = {k: payload.get(k) for k in EDITABLE if k in payload}
    _scrub_text(data)
    # 结构式优先于 TikZ（_resolve_figure 内部也是这个顺序）
    resolved = False
    if data.get("figure_flow") or data.get("figure_smiles") or data.get("figure_tikz"):
        await run_in_threadpool(_resolve_figure, data)
        _honour_needs_figure(data, payload)
        resolved = True
    # 前端会把旧的 figure_svg 一起回传，刚编译出来的不能被它盖掉
    if payload.get("figure_svg") and not resolved:
        from ..render.svg import sanitize_svg

        cleaned = sanitize_svg(str(payload["figure_svg"]))
        if cleaned:
            data["figure_svg"] = cleaned
        else:
            data.pop("figure_svg", None)
            data["needs_figure"] = True
    crop_token = str(payload.get("crop_token") or "")
    if crop_token:
        try:
            path = common.crop_path(crop_token)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="图片片段无效") from exc
        data["crop_path"] = str(path)
        data["image_id"] = payload.get("image_id")
        data["bbox"] = payload.get("bbox")
    if payload.get("ai_meta"):
        meta = payload["ai_meta"] or {}
        data["ai_model"] = meta.get("model")
        data["ai_log_id"] = meta.get("log_id")
        # 完整 raw 优先从调用日志取；前端传来的 raw_preview 只有 1500 字，仅作兜底
        raw_full = _raw_from_log(meta.get("log_id"))
        raw_in = str(payload.get("raw_ai") or "")
        data["raw_ai"] = (raw_full or raw_in)[:20000]
        data["source"] = "ai"
    box = data.pop("figure_box", None) if "figure_box" in data else _MISSING
    q = db.insert_question(data)
    if box is not _MISSING:
        await run_in_threadpool(_apply_figure_box, q["uid"], box)
        q = db.get_question_by_uid(q["uid"]) or q
    return {"question": common.question_payload(q)}


@router.get("/{uid}")
async def get_question(uid: str) -> dict[str, Any]:
    q = db.get_question_by_uid(uid)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    return {"question": common.question_payload(q)}


@router.get("/{uid}/figure.png")
async def question_figure_png(uid: str):
    """题图的 PNG（服务端栅格化，按需渲染 + 缓存）。

    前端 ``<img src>`` 直接指向这里 —— 预览、题库、导出全用同一张图，
    不再在浏览器里插 SVG（DOMPurify 那条链已经把化学键洗掉过两次）。
    """
    from fastapi.responses import FileResponse

    from ..render.svg import revive_figure_svg, svg_to_png

    q = db.get_question_by_uid(uid)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    data = common.question_payload(q)
    svg = revive_figure_svg(data)
    if not svg:
        raise HTTPException(status_code=404, detail="本题没有题图")
    path = await run_in_threadpool(svg_to_png, svg, 1200, 2)
    if path is None:
        raise HTTPException(status_code=500, detail="题图渲染失败")
    return FileResponse(path, media_type="image/png")


@router.patch("/{uid}")
async def update_question(uid: str, payload: dict[str, Any]) -> dict[str, Any]:
    q = db.get_question_by_uid(uid)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    patch = {k: v for k, v in (payload or {}).items() if k in EDITABLE}
    _scrub_text(patch)
    if "stem" in patch and not str(patch["stem"]).strip():
        raise HTTPException(status_code=400, detail="题目内容不能为空")
    box = patch.pop("figure_box", _MISSING)
    # 结构式优先于 TikZ：用户在编辑器里改了 SMILES 就重新出图
    resolved = False
    if patch.get("figure_flow") or patch.get("figure_smiles") or patch.get("figure_tikz"):
        await run_in_threadpool(_resolve_figure, patch)
        _honour_needs_figure(patch, payload)
        resolved = True
    # 前端回传的旧 svg 不能盖掉刚编译的结果
    if "figure_svg" in patch and patch["figure_svg"] and not resolved:
        from ..render.svg import sanitize_svg

        cleaned = sanitize_svg(str(patch["figure_svg"]))
        if cleaned:
            patch["figure_svg"] = cleaned
        else:
            patch.pop("figure_svg")
            patch["needs_figure"] = True
    elif "figure_svg" in patch and not patch["figure_svg"]:
        patch["figure_svg"] = None
    updated = db.update_question(int(q["id"]), patch)
    if not updated:
        raise HTTPException(status_code=404, detail="题目不存在")
    if box is not _MISSING:
        await run_in_threadpool(_apply_figure_box, uid, box)
        updated = db.get_question_by_uid(uid) or updated
    return {"question": common.question_payload(updated)}


@router.post("/{uid}/re-enhance")
async def re_enhance_figure(uid: str) -> dict[str, Any]:
    """按当前裁剪框，从**原始截图**重算增强后的题图（存量图一键翻新）。

    不依赖上一版增强结果，所以可以反复点；返回新的 ``figure_crop_url``，
    文件名带算法版本号，浏览器不会命中旧缓存。
    """
    q = db.get_question_by_uid(uid)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    if not q.get("crop_path"):
        raise HTTPException(status_code=400, detail="这道题没有原始截图，无法增强")
    box = q.get("figure_box")
    if not box:
        raise HTTPException(status_code=400, detail="还没框选图形区域")
    try:
        box = json.loads(str(box))
    except json.JSONDecodeError:
        pass
    target = await run_in_threadpool(_render_figure_crop, q, box)
    if target is None:
        raise HTTPException(status_code=400, detail="增强失败，请重新框选")
    old = q.get("figure_crop")
    if old and old != str(target):
        _drop_figure_file(old)
    db.update_question(int(q["id"]), {"figure_crop": str(target)})
    fresh = db.get_question_by_uid(uid) or q
    return {"question": common.question_payload(fresh), "ok": True}


@router.delete("/{uid}")
async def delete_question(uid: str) -> dict[str, Any]:
    q = db.get_question_by_uid(uid)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    db.execute("UPDATE questions SET deleted=1, updated_at=? WHERE id=?", (db.now(), int(q["id"])))
    return {"ok": True}


@router.post("/bulk-delete")
async def bulk_delete(payload: dict[str, Any]) -> dict[str, Any]:
    uids = [str(x) for x in (payload.get("uids") or [])]
    if not uids:
        return {"deleted": 0}
    marks = ",".join("?" * len(uids))
    rows = db.query(f"SELECT id FROM questions WHERE uid IN ({marks})", uids)
    db.execute_many(
        "UPDATE questions SET deleted=1, updated_at=? WHERE id=?",
        [(db.now(), int(r["id"])) for r in rows],
    )
    return {"deleted": len(rows)}


@router.post("/{uid}/star")
async def toggle_star(uid: str) -> dict[str, Any]:
    q = db.get_question_by_uid(uid)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    new_value = 0 if q.get("starred") else 1
    db.execute("UPDATE questions SET starred=?, updated_at=? WHERE id=?", (new_value, db.now(), int(q["id"])))
    return {"starred": bool(new_value)}
