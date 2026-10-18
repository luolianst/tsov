"""后端宿主播放域路由：play / play-stop（M-V8 E1；F5 自 tsov/web.py 拆出）。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ..live_transport import LiveUnsupported
from ..models import PlayIn, ReloadStateIn, SeekIn
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
        start = float(body.start or 0.0) if body else 0.0
        loop = None
        if body and body.loop is not None:
            if len(body.loop) != 2:
                raise HTTPException(400, "loop 需 [a, b] 两元素")
            a, b = float(body.loop[0]), float(body.loop[1])
            if a < 0 or b <= a:
                raise HTTPException(400, f"loop 区间非法：{a}/{b}")
            loop = (a, b)

        try:
            return st().live.play(proj, start=start, loop=loop)
        except LiveUnsupported as exc:
            # S2 收口：旧 wav/sounddevice 播放路径退出主链——不再回退
            raise HTTPException(409, f"实时引擎不支持该工程：{exc}（旧 wav 播放已停用）") from exc

    @app.post("/api/projects/{name}/seek")
    def seek(name: str, body: SeekIn) -> dict:
        """播放轴定位（live sidecar；未启动时回 not_started，前端仍更新本地轴）。"""
        st().get_project(name)
        return st().live.seek(name, float(body.position))

    @app.post("/api/projects/{name}/live/reload-state")
    def reload_state(name: str, body: ReloadStateIn) -> dict:
        """G1：插件编辑器关窗保存的 state → 更新 manifest 并重启 sidecar。"""
        st().get_project(name)
        return st().live.reload_editor_state(name, body.plugin, body.state_path)

    @app.post("/api/projects/{name}/play/stop")
    def play_stop(name: str) -> dict:
        """停止播放：先停 live sidecar，再兜底旧 sounddevice 路径。"""
        st().get_project(name)  # 工程存在性校验
        try:
            st().live.stop(name)
        except Exception:  # noqa: BLE001
            pass
        ev = st().play_stop
        if ev is not None:
            ev.set()
        return {"ok": True}
