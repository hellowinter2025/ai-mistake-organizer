"""FastAPI 应用入口。

启动：``python start.py`` 或 ``python -m app.main``，默认 http://127.0.0.1:8765
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import APP_TOKEN, APP_VERSION, WEB_DIR, settings
from .routes import (
    ai_routes,
    chem as chem_routes,
    figures,
    flow as flow_routes,
    images,
    papers,
    questions,
    queue,
    settings_routes,
    system,
    tikz as tikz_routes,
)
from .security import SecurityMiddleware


def create_app() -> FastAPI:
    db.init_db()
    app = FastAPI(
        title="AI 错题整理",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(SecurityMiddleware)

    app.include_router(images.router)
    app.include_router(ai_routes.router)
    app.include_router(questions.router)
    app.include_router(papers.router)
    app.include_router(settings_routes.router)
    app.include_router(system.router)
    app.include_router(tikz_routes.router)
    app.include_router(chem_routes.router)
    app.include_router(flow_routes.router)
    app.include_router(figures.router)
    app.include_router(queue.router)

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        active = settings.active_config()
        return {
            "token": APP_TOKEN,
            "version": APP_VERSION,
            "settings": settings.public_view(),
            "active": {"config_id": active[0]["id"], "model": active[1]} if active else None,
            "stats": db.stats(),
        }

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        return JSONResponse({"detail": f"服务器内部错误：{exc}"}, status_code=500)

    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


app = create_app()


def main() -> None:
    import uvicorn

    from . import runtime

    host = settings.get("server", "host", default="127.0.0.1")
    port = int(settings.get("server", "port", default=8765))
    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    # 网页上的「关闭服务」→ server.should_exit = True → 干净退出
    runtime.on_shutdown(lambda: setattr(server, "should_exit", True))
    server.run()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main()
