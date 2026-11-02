"""识别队列的持久化接口（替代 localStorage）。

队列是解析结果的暂存区，以前放浏览器 localStorage：5MB 配额会把 AI 原始响应
截断、两个标签页互相覆盖、换机器就丢。现在整份存在服务端 SQLite，前端只管
全量拉取 / 全量写回。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import db

router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("")
async def get_queue() -> dict[str, Any]:
    items = db.queue_list()
    return {"items": items, "total": len(items)}


@router.put("")
async def put_queue(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    # 只认对象条目，脏数据（null / 字符串）直接丢掉，不让它污染队列
    clean = [it for it in items if isinstance(it, dict)]
    db.queue_replace(clean)
    return {"ok": True, "total": len(clean)}
