"""FastAPI 组装（F5：create_app 自 tsov/web.py 迁入；薄壳 web.py 仅再导出 + CLI 入口）。

组装顺序 = 原 web.py：异常处理器 → 域路由（basic→projects→render→audio→retention→playback→events→chat）
→ 静态前端最后挂载（/api 路由先注册，不受影响）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .routes import ROUTE_MODULES
from .state import WebState


def create_app(output_dir: str | Path = "output") -> FastAPI:
    app = FastAPI(title="the shape of voice — 可视化宿主", version=config.WEB_VERSION)
    app.state.tsov = WebState(output_dir)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        # 契约统一：错误返回 {"error": "<中文描述>"}（docs/05 §三）
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": f"内部错误：{type(exc).__name__}: {exc}"}, status_code=500)

    for mod in ROUTE_MODULES:
        mod.register(app)

    # ---------------- 静态前端（最后挂载；/api 路由先注册，不受影响） ----------------

    if config.STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")

    return app
