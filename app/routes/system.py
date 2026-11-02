"""系统控制：网页上的「关闭服务」。

只有本机页面能调（服务默认只监听 127.0.0.1，且写请求要带 X-App-Token），
触发的是 uvicorn 的优雅退出，不会硬杀进程。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import runtime

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/system")
async def system_info() -> dict[str, Any]:
    return {"can_shutdown": runtime.scheduled()}


@router.post("/shutdown")
async def shutdown() -> dict[str, Any]:
    scheduled = runtime.request_shutdown()
    return {
        "ok": True,
        # 裸跑（python -m app.main 之外的测试环境）时没有回调可触发，
        # 前端据此提示「当前实例不是由启动器拉起的」
        "scheduled": scheduled,
    }
