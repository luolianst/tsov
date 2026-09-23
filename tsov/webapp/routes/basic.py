"""基础路由：健康检查 / 面板元数据（F5 自 tsov/web.py 拆出）。"""

from __future__ import annotations

from fastapi import FastAPI

from .. import config
from ..state import WebState


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": config.WEB_VERSION}

    @app.get("/api/meta")
    def meta() -> dict:
        """UI 批A：面板元数据（GM 音色名 / 效果类型）。"""
        from ...host.effect import effect_kinds
        from ...midi.export import GM_PROGRAMS

        return {"programs": sorted(GM_PROGRAMS), "effect_kinds": effect_kinds()}
