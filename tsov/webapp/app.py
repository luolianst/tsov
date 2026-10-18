"""FastAPI 组装（F5：create_app 自 tsov/web.py 迁入；薄壳 web.py 仅再导出 + CLI 入口）。

组装顺序 = 原 web.py：异常处理器 → 域路由（basic→projects→render→audio→retention→playback→events→chat）
→ 静态前端最后挂载（/api 路由先注册，不受影响）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..host import vst_worker
from . import config
from .routes import ROUTE_MODULES
from .state import WebState


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """批L L1-2：插件 worker 生命周期挂服务侧（懒启动；服务退出时收尾）。"""
    yield
    try:
        _app.state.tsov.live.close()
    except Exception:  # noqa: BLE001
        pass
    vst_worker.shutdown_global()


def create_app(output_dir: str | Path = "output") -> FastAPI:
    app = FastAPI(title="the shape of voice — 可视化宿主", version=config.WEB_VERSION, lifespan=_lifespan)
    app.state.tsov = WebState(output_dir)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        # 契约统一：错误返回 {"error": "<中文描述>"}（docs/05 §三）
        # P62a（10-14）增补：detail 为 dict 时原样透传（结构化错误，如 instrument_silent + warnings）
        if isinstance(exc.detail, dict):
            return JSONResponse(exc.detail, status_code=exc.status_code)
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """C4：422 也走统一错误契约（{"error": ...}，不再是 FastAPI 默认 detail 数组）。"""
        parts = []
        for e in exc.errors()[:8]:
            loc = ".".join(str(x) for x in (e.get("loc") or []) if x != "body")
            parts.append(f"{loc or 'body'}: {e.get('msg')}")
        return JSONResponse({"error": "请求参数不合法：" + "；".join(parts),
                             "details": exc.errors()}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": f"内部错误：{type(exc).__name__}: {exc}"}, status_code=500)

    for mod in ROUTE_MODULES:
        mod.register(app)

    # ---------------- 静态前端（最后挂载；/api 路由先注册，不受影响） ----------------

    # 零构建开发件：js/css/html 加 no-cache（仍带 ETag → 命中 304）。
    # 防浏览器启发式缓存把旧 JS 静默喂给长开页面（2026-10-12 实测坑：改码后页面拿不到新代码）。
    @app.middleware("http")
    async def _static_no_cache(request: Request, call_next):
        resp = await call_next(request)
        p = request.url.path
        if p.startswith(("/js/", "/css/")) or p == "/" or p.endswith(".html"):
            resp.headers.setdefault("Cache-Control", "no-cache")
        return resp

    if config.STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")

    return app
