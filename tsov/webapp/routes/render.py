"""渲染 / 回放 / 导出域路由：render / wav / cache-gc / export（F5 自 tsov/web.py 拆出）。

M-V7 D1：轨道级 freeze + 统一 stem 库（ADR-0018）。函数体逐行保留。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ...host.cache import StemStore, score_keys   # M-V7 D1（ADR-0018）：轨道级 freeze / stem 库
from .. import config
from ..models import ExportIn, RenderIn
from ..state import WebState


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    def _assemble(name: str, proj, score, out_path, *, rev: str | None = None) -> dict:
        """按 stem 库拼装混音（缺轨渲后入库）；逐轨进度上 SSE。返回统计。"""
        engine = st().engine()
        store = StemStore(proj.root)
        stats: dict = {}
        bus = st().bus

        def on_progress(ev: dict) -> None:
            bus.publish(name, "render_progress", ev)

        session = engine.load(score, base_dir=proj.root)
        try:
            audio = engine.render(session, out_wav=out_path, stereo=True,
                                  cache=store, stats=stats, on_progress=on_progress)
        finally:
            session.close()
        info = {
            "rendered": stats.get("rendered", []),
            "cached": stats.get("cached", []),
            "duration": round(audio.shape[0] / engine.samplerate, 3),
        }
        bus.publish(name, "render_done", {"rendered": info["rendered"], "cached": len(info["cached"]),
                                          **({"rev": rev[:8]} if rev else {})})
        return info

    @app.post("/api/projects/{name}/render")
    def render(name: str, body: RenderIn) -> dict:
        proj = st().get_project(name)
        engine = st().engine()
        if body.rev:
            # 任意 git 版本：按 manifest 拼装（缺轨渲后入库；不进 rollback）
            score = proj.score_at(body.rev)
            if score is None:
                raise HTTPException(400, f"版本不存在或内容损坏：{body.rev!r}")
            mix_dir = proj.root / ".mix-cache"
            mix_dir.mkdir(exist_ok=True)
            out = mix_dir / f"{body.rev[:8]}.wav"
            info = _assemble(name, proj, score, out, rev=body.rev)
            return {"wav": str(out), "sr": engine.samplerate, "rev": body.rev[:8], **info}
        out = Path(body.out) if body.out else proj.root / config.RENDER_WAV_NAME
        proj.save()  # score.json 落最新真相（渲染与 git 版本一致）
        info = _assemble(name, proj, proj.score, out)
        return {"wav": str(out), "sr": engine.samplerate, **info}

    @app.get("/api/projects/{name}/wav")
    def wav(name: str, rev: str | None = None):
        """浏览器试听 wav（ADR-0018：stem 库拼装；缺失/比 score.json 旧时自动重拼）。

        ?rev=<版本>：按该版本拼装（git show，不动 HEAD），用于对比滑块左槽试听。
        """
        from fastapi.responses import FileResponse

        proj = st().get_project(name)
        if rev:
            score = proj.score_at(rev)
            if score is None:
                raise HTTPException(400, f"版本不存在或内容损坏：{rev!r}")
            mix_dir = proj.root / ".mix-cache"
            mix_dir.mkdir(exist_ok=True)
            wav_path = mix_dir / f"{rev[:8]}.wav"
            if not wav_path.is_file():
                _assemble(name, proj, score, wav_path, rev=rev)
            return FileResponse(str(wav_path), media_type="audio/wav", filename=f"{name}-{rev[:8]}.wav")

        wav_path = proj.root / config.RENDER_WAV_NAME
        score_path = proj.root / "score.json"
        stale = not wav_path.is_file() or score_path.stat().st_mtime > wav_path.stat().st_mtime
        if stale:
            _assemble(name, proj, proj.score, wav_path)
        return FileResponse(str(wav_path), media_type="audio/wav", filename=f"{name}.wav")

    # ---------------- stem 缓存 GC（M-V7 D1 / ADR-0018） ----------------

    @app.post("/api/projects/{name}/cache/gc")
    def cache_gc(name: str) -> dict:
        """删除不被 HEAD/收藏版本引用的 stem；顺带清理退役的 `.render-cache/`。"""
        proj = st().get_project(name)
        engine = st().engine()
        store = StemStore(proj.root)
        keep = set(score_keys(proj.score, engine.samplerate))
        for fav in proj.favorites():
            s = proj.score_at(fav.get("tag") or "HEAD")
            if s is not None:
                keep |= score_keys(s, engine.samplerate)
        report = store.gc(keep)
        report["keep"] = len(keep)
        report["size_mb"] = round(store.size_bytes() / 1024 / 1024, 2)
        return report

    # ---------------- 导出（修正轮2；F2 后经 HostEngine.export） ----------------

    @app.post("/api/projects/{name}/export")
    def export(name: str, body: ExportIn) -> dict:
        """导出矩阵（master / 总线 / 每轨 stems + MIDI）→ 工程 exports/<时间戳>/。"""
        proj = st().get_project(name)
        proj.save()
        engine = st().engine()
        out_dir = proj.root / "exports" / datetime.now().strftime("%Y%m%d-%H%M%S")
        session = engine.load(proj.score, base_dir=proj.root)
        try:
            report = engine.export(session, out_dir,
                                   mix=body.mix, buses=body.buses, stems=body.stems,
                                   midi=body.midi, midi_stems=body.midi_stems,
                                   cache=StemStore(proj.root))
        finally:
            session.close()
        return report
