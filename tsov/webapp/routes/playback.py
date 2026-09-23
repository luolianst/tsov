"""后端宿主播放域路由：play / play-stop（M-V8 E1；F5 自 tsov/web.py 拆出）。"""

from __future__ import annotations

import threading

from fastapi import FastAPI, HTTPException

from ..helpers import score_duration
from ..models import PlayIn
from ..state import WebState


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    @app.post("/api/projects/{name}/play")
    def play(name: str, body: PlayIn | None = None) -> dict:
        """后端宿主播放（sounddevice 阻塞到播完；一次只跑一个，忙时 409）。

        M-V8 E1：body.start = 起始秒（播放轴定位）；body.loop = [a, b] 区间循环
        （循环时阻塞直到 `/play/stop`）。
        """
        proj = st().get_project(name)
        if not st().play_lock.acquire(blocking=False):
            raise HTTPException(409, "已有播放在进行中")
        bus = st().bus
        duration = score_duration(proj.score, proj.root)
        start = float(body.start or 0.0) if body else 0.0
        loop = None
        if body and body.loop is not None:
            if len(body.loop) != 2:
                st().play_lock.release()
                raise HTTPException(400, "loop 需 [a, b] 两元素")
            a, b = float(body.loop[0]), float(body.loop[1])
            if a < 0 or b <= a:
                st().play_lock.release()
                raise HTTPException(400, f"loop 区间非法：{a}/{b}")
            loop = (a, b)
        st().play_stop = threading.Event()
        try:
            bus.publish(name, "playback_start", {"duration": duration, "start": start,
                                                 "loop": list(loop) if loop else None})
            engine = st().engine()
            engine.play(engine.load(proj.score, base_dir=proj.root), blocking=True, start=start, loop=loop,
                        stop_event=st().play_stop)
            bus.publish(name, "playback_stop", {"duration": duration})
            return {"ok": True, "duration": duration, "start": start,
                    "loop": list(loop) if loop else None}
        finally:
            st().play_stop = None
            st().play_lock.release()

    @app.post("/api/projects/{name}/play/stop")
    def play_stop(name: str) -> dict:
        """停止宿主播放（M-V8 E1）：置停止信号 + 打断 sounddevice。"""
        st().get_project(name)  # 工程存在性校验
        ev = st().play_stop
        if ev is not None:
            ev.set()
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001 —— 无设备/未装时静默（无播放可停）
            pass
        return {"ok": True}
