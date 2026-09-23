"""SSE 事件流域路由（F5 自 tsov/web.py 拆出）。

心跳间隔经 `config.SSE_HEARTBEAT_SEC` 动态读取（测试打桩点）。
"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import config
from ..state import WebState


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    @app.get("/api/projects/{name}/events")
    async def events(name: str, request: Request, close_after: int | None = None) -> StreamingResponse:
        """SSE 事件流。close_after=N：产出 N 帧后正常收流（测试用；浏览器缺省无限流）。"""
        state = st()
        if not state.project_exists(name):
            raise HTTPException(404, f"工程不存在：{name!r}")
        sub = state.bus.subscribe(name)

        async def gen():
            frames = 0
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(sub.queue.get(), timeout=config.SSE_HEARTBEAT_SEC)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        frames += 1
                    else:
                        yield (
                            f"event: {msg['event']}\n"
                            f"data: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                        )
                        frames += 1
                    if close_after is not None and frames >= close_after:
                        break
            finally:
                state.bus.unsubscribe(name, sub)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )
