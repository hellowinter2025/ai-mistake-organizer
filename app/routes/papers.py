"""试卷组装、预览与导出。"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from .. import db, desktop
from ..config import settings
from ..render import docx_export, pdf
from . import common

router = APIRouter(prefix="/api/papers", tags=["papers"])

ALLOWED_OPTIONS = {
    "show_answer",
    "show_error_reason",
    "show_analysis",
    "answer_position",
    "include_source_image",
    "answer_space",
    "show_knowledge_points",
    "layout",
}

# layout:
#   standard —— A4 整卷，题干 + 错因 + 解析 + 答案
#   cutout  —— 裁切粘贴版：只有题目和错因，页宽比 16K 略窄，方便剪下来贴进笔记本
LAYOUTS = ("standard", "cutout")

DEFAULTS = {
    "show_answer": True,
    "show_error_reason": True,
    "show_analysis": True,
    "answer_position": "inline",
    "include_source_image": False,
    "answer_space": 0,
    "show_knowledge_points": False,
    "layout": "standard",
}


def _clean_options(raw: dict[str, Any] | None) -> dict[str, Any]:
    opts = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if key in ALLOWED_OPTIONS:
            opts[key] = value
    if opts.get("answer_position") not in ("inline", "end"):
        opts["answer_position"] = "inline"
    if opts.get("layout") not in LAYOUTS:
        opts["layout"] = "standard"
    try:
        opts["answer_space"] = max(0, min(6, int(opts.get("answer_space") or 0)))
    except (TypeError, ValueError):
        opts["answer_space"] = 0
    return opts


def _default_title() -> str:
    """默认卷名：当前日期 + 错题，例如「10月29日错题」。"""
    today = date.today()
    return f"{today.month}月{today.day}日错题"


def _safe_filename(text: str, fallback: str = "错题练习") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", (text or "").strip())
    cleaned = cleaned.strip("._ ") or fallback
    return cleaned[:60]


# 排版方式 → 文件名后缀。裁切粘贴版会剪成 16 开，和整卷版长得不一样，
# 放同一个下载目录里必须一眼能分开，否则导出几次就分不清哪份是哪个版式。
LAYOUT_SUFFIX = {"cutout": "裁切"}


def _export_name(paper: dict[str, Any], ext: str) -> str:
    """导出文件名：卷名 [+ 版式后缀] . 扩展名。"""
    base = _safe_filename(paper.get("title"))
    opts = paper.get("options") or {}
    if not isinstance(opts, dict):
        opts = {}
    suffix = LAYOUT_SUFFIX.get(str(opts.get("layout") or ""))
    if suffix:
        base = f"{base}-{suffix}"
    return f"{base}.{ext}"


@router.get("")
async def list_papers() -> dict[str, Any]:
    return {"items": db.list_papers()}


@router.post("")
async def create_paper(payload: dict[str, Any]) -> dict[str, Any]:
    uids = [str(x) for x in (payload.get("question_uids") or [])]
    if not uids:
        raise HTTPException(status_code=400, detail="请至少选择一道题")
    rows = []
    for uid in uids:
        q = db.get_question_by_uid(uid)
        if q:
            rows.append(q)
    if not rows:
        raise HTTPException(status_code=400, detail="题目不存在或已删除")
    paper = db.insert_paper(
        {
            "title": (payload.get("title") or "").strip() or _default_title(),
            "subtitle": payload.get("subtitle") or "",
            "source": (payload.get("source") or "").strip()[:120],
            "subject": (payload.get("subject") or "").strip()[:20]
            or settings.get("general", "subject", default="数学"),
            "student_name": payload.get("student_name") or "",
            "exam_date": payload.get("exam_date") or date.today().isoformat(),
            "options": _clean_options(payload.get("options")),
        },
        [int(q["id"]) for q in rows],
    )
    return {"paper": paper}


@router.put("/{uid}")
async def update_paper(uid: str, payload: dict[str, Any]) -> dict[str, Any]:
    """更新已保存的试卷：改标题/选项/题目时原地更新，不再新建一份。"""
    paper = _by_uid(uid)
    uids = [str(x) for x in (payload.get("question_uids") or [])]
    fields: dict[str, Any] = {
        "title": (payload.get("title") or "").strip() or _default_title(),
        "subtitle": payload.get("subtitle") or "",
        "source": (payload.get("source") or "").strip()[:120],
        "subject": (payload.get("subject") or "").strip()[:20]
        or settings.get("general", "subject", default="数学"),
        "student_name": payload.get("student_name") or "",
        "exam_date": payload.get("exam_date") or date.today().isoformat(),
        "options": _clean_options(payload.get("options")),
    }
    question_ids: list[int] | None = None
    if uids:
        rows = [db.get_question_by_uid(u) for u in uids]
        question_ids = [int(r["id"]) for r in rows if r]
        if not question_ids:
            raise HTTPException(status_code=400, detail="题目不存在或已删除")
    updated = db.update_paper(int(paper["id"]), fields, question_ids)
    if not updated:
        raise HTTPException(status_code=404, detail="试卷不存在")
    return {"paper": updated}


@router.get("/{uid}")
async def get_paper(uid: str) -> dict[str, Any]:
    paper = _by_uid(uid)
    questions = [common.question_payload(q) for q in db.paper_questions(int(paper["id"]))]
    return {"paper": paper, "questions": questions}


@router.delete("/{uid}")
async def delete_paper(uid: str) -> dict[str, Any]:
    paper = _by_uid(uid)
    db.delete_paper(int(paper["id"]))
    return {"ok": True}


@router.get("/{uid}/preview", response_class=HTMLResponse)
async def preview(uid: str) -> HTMLResponse:
    paper = _by_uid(uid)
    questions = db.paper_questions(int(paper["id"]))
    html_doc = await run_in_threadpool(pdf.build_paper_html, paper, questions)
    return HTMLResponse(html_doc)


async def _render_export(paper: dict[str, Any], fmt: str) -> tuple[Path, str, str]:
    """生成导出文件，返回 (路径, 扩展名, MIME)。"""
    questions = db.paper_questions(int(paper["id"]))
    if not questions:
        raise HTTPException(status_code=400, detail="试卷中没有题目")
    try:
        if fmt == "pdf":
            path = await run_in_threadpool(pdf.export_pdf, paper, questions)
            return path, "pdf", "application/pdf"
        path = await run_in_threadpool(docx_export.build_docx, paper, questions)
        return (
            path,
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{uid}/export")
async def export(uid: str, fmt: str = Query("pdf", pattern="^(pdf|docx)$")) -> FileResponse:
    """浏览器下载（保留兼容）。日常用 POST，直接落到下载文件夹。"""
    paper = _by_uid(uid)
    path, ext, media = await _render_export(paper, fmt)
    name = _export_name(paper, ext)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"}
    return FileResponse(path, media_type=media, headers=headers)


@router.post("/{uid}/export")
async def export_to_downloads(
    uid: str, fmt: str = Query("pdf", pattern="^(pdf|docx)$")
) -> dict[str, Any]:
    """生成后直接写进本机下载文件夹，并（在没开着时）打开它。

    绕开浏览器下载：不触发安全扫描、不用去下载列表里找文件。
    """
    paper = _by_uid(uid)
    path, ext, _media = await _render_export(paper, fmt)
    name = _export_name(paper, ext)
    try:
        dest = await run_in_threadpool(desktop.publish, path, name)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    opened = await run_in_threadpool(desktop.reveal_folder, dest.parent)
    return {
        "ok": True,
        "name": dest.name,
        "path": str(dest),
        "dir": str(dest.parent),
        "opened": opened,
    }


def _by_uid(uid: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM papers WHERE uid=? AND deleted=0", (uid,))
    if not row:
        raise HTTPException(status_code=404, detail="试卷不存在")
    paper = dict(row)
    try:
        import json

        paper["options"] = json.loads(paper.get("options") or "{}")
    except ValueError:
        paper["options"] = {}
    return paper
