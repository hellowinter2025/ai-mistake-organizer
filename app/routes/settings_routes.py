"""设置、状态与配置管理。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import db
from ..ai import client as ai_client
from ..config import APP_VERSION, STARTED_AT, settings

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/state")
async def state() -> dict[str, Any]:
    active = settings.active_config()
    return {
        "version": APP_VERSION,
        "started_at": STARTED_AT,
        "settings": settings.public_view(),
        "active": {"config_id": active[0]["id"], "model": active[1]} if active else None,
        "stats": db.stats(),
        "fonts": {"body": _font_available("宋体"), "title": _font_available("方正小标宋简体")},
    }


@router.patch("/settings")
async def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"ai", "segment", "export", "general"}
    patch = {k: v for k, v in (payload or {}).items() if k in allowed and isinstance(v, dict)}
    if not patch:
        raise HTTPException(status_code=400, detail="没有可更新的设置")
    if "general" in patch:
        from ..ai.prompts import normalise_subject

        general = patch["general"]
        general["subject"] = normalise_subject(general.get("subject"))
    if "ai" in patch:
        ai_patch = patch["ai"]
        if "concurrency" in ai_patch:
            ai_patch["concurrency"] = max(1, min(6, int(ai_patch["concurrency"])))
        if "active_config_id" in ai_patch:
            cid = ai_patch["active_config_id"]
            if cid is not None and not settings.config_by_id(int(cid)):
                raise HTTPException(status_code=400, detail="配置不存在")
        if "active_model" in ai_patch and ai_patch["active_model"] in ("", None):
            ai_patch["active_model"] = None
    settings.update(patch)
    return {"settings": settings.public_view()}


@router.post("/settings/test")
async def test_config(payload: dict[str, Any]) -> dict[str, Any]:
    config_id = payload.get("config_id")
    if config_id is None:
        raise HTTPException(status_code=400, detail="请选择配置")
    try:
        result = await ai_client.test_config(int(config_id), payload.get("model"))
    except ai_client.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


@router.get("/settings/models")
async def models(config_id: int) -> dict[str, Any]:
    try:
        items = await ai_client.list_models(config_id)
    except ai_client.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"models": items}


@router.post("/settings/models")
async def save_models(payload: dict[str, Any]) -> dict[str, Any]:
    """把从接口拉到的模型列表写回配置，方便下次不用再拉。"""
    config_id = payload.get("config_id")
    models = [str(m) for m in (payload.get("models") or [])][:200]
    cfg = settings.config_by_id(int(config_id)) if config_id is not None else None
    if not cfg:
        raise HTTPException(status_code=400, detail="配置不存在")
    configs = settings.configs
    for item in configs:
        if int(item.get("id", -1)) == int(config_id):
            item["models"] = models
    settings.set(configs, "configs")
    return {"models": models}


@router.post("/settings/configs")
async def create_config(payload: dict[str, Any]) -> dict[str, Any]:
    """直接新增一条 AI 服务配置（不依赖界面里的「新增服务」弹层）。

    返回脱敏后的这一条（含 ``has_key`` / ``key_hint``），前端不需要再取一次全量。
    """
    try:
        cfg = settings.save_config(payload or {}, None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config": cfg, "settings": settings.public_view()}


@router.patch("/settings/configs/{config_id}")
async def update_config(config_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """改一条配置。

    ``api_key`` 不传或传空串 = 沿用原来的 key（前端拿不到明文，也不该被逼着重填）。
    传 ``null`` 才是显式清空。``models`` 不传 = 不动缓存列表。
    """
    try:
        cfg = settings.save_config(payload or {}, config_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="配置不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config": cfg, "settings": settings.public_view()}


@router.delete("/settings/configs/{config_id}")
async def delete_config(config_id: int) -> dict[str, Any]:
    if not settings.delete_config(config_id):
        raise HTTPException(status_code=404, detail="配置不存在")
    return {"settings": settings.public_view()}


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": APP_VERSION}


def _font_available(name: str) -> bool:
    try:
        from ..render.docx_export import _installed_fonts

        fonts = _installed_fonts()
        return (not fonts) or any(name in f for f in fonts)
    except Exception:  # noqa: BLE001
        return False
