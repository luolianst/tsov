"""P83：显式 freeze/unfreeze 路由。

冻结顺序（OP1）：停播 → sidecar 失效 → 离线渲染 BK1 资产 → 命令层写 freeze 状态。
解冻：命令层清状态 → sidecar 失效；资产只解 pin，物理删除交 `cache/gc`。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query

from ...host.cache import StemStore
from ...host.command import EditBatch
from ...host.freeze import FreezeError, freeze_state, render_freeze_asset
from ..helpers import project_state
from ..models import FreezeIn, UnfreezeIn
from ..state import WebState


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    def _project_and_track(name: str, index: int):
        proj = st().get_project(name)
        if not (0 <= int(index) < len(proj.score.tracks)):
            raise HTTPException(404, f"轨道不存在：{index}（共 {len(proj.score.tracks)} 轨）")
        return proj, proj.score.tracks[int(index)]

    def _playback_snapshot(name: str) -> tuple[bool, int]:
        try:
            state = st().live.state()
            if not state.get("ok"):
                return False, 0
            return bool(state.get("playing")), int(state.get("playhead", 0) or 0)
        except Exception:  # noqa: BLE001
            return False, 0

    def _invalidate_live(name: str) -> None:
        try:
            st().live.stop(name)
        except Exception:  # noqa: BLE001
            pass
        try:
            st().live.invalidate(name)
        except Exception:  # noqa: BLE001
            pass

    @app.get("/api/projects/{name}/freeze")
    def freeze_info(name: str, track: int = Query(...)) -> dict:
        proj, tr = _project_and_track(name, track)
        store = StemStore(proj.root)
        status = freeze_state(tr, proj.score, store, int(st().engine().samplerate))
        return {"ok": True, "track": int(track), "name": tr.name, **status,
                "freeze": dict(tr.freeze) if tr.freeze else None}

    @app.post("/api/projects/{name}/freeze")
    def freeze(name: str, body: FreezeIn) -> dict:
        proj, tr = _project_and_track(name, body.track)
        if tr.freeze and not body.force:
            raise HTTPException(409, {"error": f"轨道「{tr.name}」已冻结（重冻需 force=true）",
                                      "state": "frozen", "freeze": dict(tr.freeze)})
        was_playing, playhead = _playback_snapshot(name)
        _invalidate_live(name)

        try:
            meta = render_freeze_asset(
                proj, body.track, samplerate=int(st().engine().samplerate),
                store=StemStore(proj.root),
            )
        except FreezeError as exc:
            raise HTTPException(400, {"error": str(exc), "state": "error", "track": body.track}) from exc
        except Exception as exc:  # noqa: BLE001 —— 插件/采样库失败转结构化错误，不留在裸 500
            raise HTTPException(400, {
                "error": f"冻结渲染失败：{type(exc).__name__}: {exc}",
                "state": "error", "track": body.track,
            }) from exc

        batch = EditBatch("冻结轨道")
        batch.add("freeze_track", track=int(body.track), value={**meta, "force": bool(body.force)})
        result = proj.apply_batch(batch, f"冻结轨道：{tr.name}")
        if not result.get("ok"):
            raise HTTPException(400, {
                "error": "冻结状态写入被拒：" + "；".join(result.get("errors") or []),
                "state": "error", "track": body.track,
            })
        st().bus.publish(name, "state_updated", project_state(proj))
        frozen_track = proj.score.tracks[int(body.track)]
        resumed = False
        if was_playing:
            try:
                st().live.play(proj, start=float(playhead) / float(st().engine().samplerate))
                resumed = True
            except Exception:  # noqa: BLE001 —— 恢复失败不回滚冻结，前端提示手动按播放
                resumed = False
        return {
            "ok": True,
            "state": "frozen",
            "track": int(body.track),
            "name": frozen_track.name,
            "freeze": dict(frozen_track.freeze or {}),
            "was_playing": was_playing,
            "resumed": resumed,
            "playhead_samples": playhead,
        }

    @app.post("/api/projects/{name}/unfreeze")
    def unfreeze(name: str, body: UnfreezeIn) -> dict:
        proj, tr = _project_and_track(name, body.track)
        if not tr.freeze:
            raise HTTPException(409, {"error": f"轨道「{tr.name}」未冻结", "state": "unfrozen"})
        was_playing, playhead = _playback_snapshot(name)
        _invalidate_live(name)
        batch = EditBatch("解冻轨道")
        batch.add("unfreeze_track", track=int(body.track))
        result = proj.apply_batch(batch, f"解冻轨道：{tr.name}")
        if not result.get("ok"):
            raise HTTPException(400, {
                "error": "解冻被拒：" + "；".join(result.get("errors") or []),
                "state": "frozen", "track": body.track,
            })
        st().bus.publish(name, "state_updated", project_state(proj))
        resumed = False
        if was_playing:
            try:
                st().live.play(proj, start=float(playhead) / float(st().engine().samplerate))
                resumed = True
            except Exception:  # noqa: BLE001
                resumed = False
        return {
            "ok": True,
            "state": "unfrozen",
            "track": int(body.track),
            "name": tr.name,
            "was_playing": was_playing,
            "resumed": resumed,
            "playhead_samples": playhead,
        }
