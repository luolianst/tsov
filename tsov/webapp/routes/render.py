"""渲染 / 回放 / 导出域路由：render / wav / cache-gc / export（F5 自 tsov/web.py 拆出）。

M-V7 D1：轨道级 freeze + 统一 stem 库（ADR-0018）。函数体逐行保留。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ...host.cache import StemStore, score_keys   # M-V7 D1（ADR-0018）：轨道级 freeze / stem 库
from ...host.diagnostics import scan_silent_instruments   # P62a（10-14）：插件静音体检
from .. import config
from ..models import ExportIn, RenderIn
from ..state import WebState


def _pre_export_silence_check(engine, proj) -> list[dict]:
    """P62a：导出前强制体检——「有音符但无输出」的 VST3 轨拦截用。

    无 `vst3:` 轨（或全部无音符）→ 零成本直通（不渲染）。否则复用 stem 库整场渲染
    （未缓存轨渲后入库；缓存命中为亚秒级）→ 采集 stem 峰值 → 扫描。
    """
    score = proj.score
    if not any(str(getattr(t.instrument, "program", "")).startswith("vst3:") and len(t.notes) > 0
               for t in score.tracks):
        return []
    stats: dict = {}
    session = engine.load(score, base_dir=proj.root)
    try:
        engine.render(session, cache=StemStore(proj.root), stats=stats)
    finally:
        session.close()
    return scan_silent_instruments(score, stats.get("stem_peaks") or {})


def _score_digest(score) -> str:
    """谱面内容摘要（同内容 → 同摘要；用于渲染结果合并）。"""
    blob = json.dumps(score.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    def _merged(payload: dict) -> dict:
        """合并命中：本次没有真渲；所有轨都视为缓存命中（含上次渲染入库的轨）。"""
        cached = list(payload.get("cached") or [])
        for tname in payload.get("rendered") or []:
            if tname not in cached:
                cached.append(tname)
        return {**payload, "rendered": [], "cached": cached}

    def _assemble(name: str, proj, score, out_path, *, rev: str | None = None) -> dict:
        """按 stem 库拼装混音（缺轨渲后入库）；逐轨进度上 SSE。返回统计。

        P0 A1：写同目录临时件，成功后 os.replace 原子替换——渲染失败不留半截 wav。
        """
        engine = st().engine()
        store = StemStore(proj.root)
        stats: dict = {}
        bus = st().bus

        def on_progress(ev: dict) -> None:
            bus.publish(name, "render_progress", ev)

        out_path = Path(out_path)
        # 保持 .wav 后缀在末尾（soundfile 按扩展名推断格式）
        tmp = out_path.with_name(f"{out_path.stem}.tmp-{os.getpid()}-{threading.get_ident()}{out_path.suffix}")
        try:
            session = engine.load(score, base_dir=proj.root)
            try:
                audio = engine.render(session, out_wav=tmp, stereo=True,
                                      cache=store, stats=stats, on_progress=on_progress)
            finally:
                session.close()
            os.replace(tmp, out_path)
        finally:
            tmp.unlink(missing_ok=True)   # 成功=已是空操作；失败=清半截临时件
        info = {
            "rendered": stats.get("rendered", []),
            "cached": stats.get("cached", []),
            "duration": round(audio.shape[0] / engine.samplerate, 3),
            # P62a（10-14）：插件静音体检（有音符但全零的 vst3 轨 → 推给用户/agent）
            "warnings": scan_silent_instruments(score, stats.get("stem_peaks") or {}),
        }
        bus.publish(name, "render_done", {"rendered": info["rendered"], "cached": len(info["cached"]),
                                          **({"rev": rev[:8]} if rev else {})})
        return info

    @app.post("/api/projects/{name}/render")
    def render(name: str, body: RenderIn) -> dict:
        proj = st().get_project(name)
        engine = st().engine()
        jobs = st().render_jobs
        if body.rev:
            # 任意 git 版本：按 manifest 拼装（缺轨渲后入库；不进 rollback）
            score = proj.score_at(body.rev)
            if score is None:
                raise HTTPException(400, f"版本不存在或内容损坏：{body.rev!r}")
            mix_dir = proj.root / ".mix-cache"
            mix_dir.mkdir(exist_ok=True)
            out = mix_dir / f"{body.rev[:8]}.wav"
            key = ("rev", body.rev[:8], str(out))
            with jobs.lock(name):
                hit = jobs.get_done(name, key)
                if hit is not None:
                    return {**_merged(hit), "rev": body.rev[:8]}
                info = _assemble(name, proj, score, out, rev=body.rev)
                payload = {"wav": str(out), "sr": engine.samplerate, **info}
                jobs.put_done(name, key, payload)
            return {**payload, "rev": body.rev[:8]}
        out = Path(body.out) if body.out else proj.root / config.RENDER_WAV_NAME
        with jobs.lock(name):
            proj.save()  # score.json 落最新真相（渲染与 git 版本一致）
            key = ("head", str(out), _score_digest(proj.score))
            hit = jobs.get_done(name, key)
            if hit is not None:
                return _merged(hit)
            info = _assemble(name, proj, proj.score, out)
            payload = {"wav": str(out), "sr": engine.samplerate, **info}
            jobs.put_done(name, key, payload)
        return payload

    @app.get("/api/projects/{name}/wav")
    def wav(name: str, rev: str | None = None):
        """浏览器试听 wav（只读；P0 A1：不再在 GET 里隐式重渲）。

        ?rev=<版本>：取该版本已拼装的 wav（404 = 尚未渲染，需先 POST /render 带 rev）。
        HEAD：缺失 → 404；比 score.json 旧 → 409（stale=true）——由前端触发 POST /render。
        """
        from fastapi.responses import FileResponse

        proj = st().get_project(name)
        if rev:
            mix_dir = proj.root / ".mix-cache"
            wav_path = mix_dir / f"{rev[:8]}.wav"
            if not wav_path.is_file():
                raise HTTPException(404, {"error": f"该版本尚未渲染：{rev[:8]}（先 POST /render 带 rev）",
                                          "rev": rev[:8], "stale": True})
            return FileResponse(str(wav_path), media_type="audio/wav", filename=f"{name}-{rev[:8]}.wav")

        wav_path = proj.root / config.RENDER_WAV_NAME
        score_path = proj.root / "score.json"
        if not wav_path.is_file():
            raise HTTPException(404, {"error": "尚未渲染——先 POST /api/projects/<name>/render", "stale": True})
        if score_path.stat().st_mtime_ns > wav_path.stat().st_mtime_ns:
            raise HTTPException(409, {"error": "wav 已过期（谱面有改动）——先 POST /render 重渲", "stale": True})
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
        # P62a（10-14）：导出前强制体检——命中静音 VST3 轨 → 拦下（推卡）；allow_silent=true 可强制
        if not body.allow_silent:
            warns = _pre_export_silence_check(engine, proj)
            if warns:
                raise HTTPException(409, {
                    "error": "存在「有音符但无输出」的 VST3 轨——请先处理（打开插件界面完成交互）再导出；"
                             "或在导出选项里勾选「强制导出」。",
                    "warnings": warns,
                })
        out_dir = proj.root / "exports" / datetime.now().strftime("%Y%m%d-%H%M%S")
        session = engine.load(proj.score, base_dir=proj.root)
        try:
            report = engine.export(session, out_dir,
                                   mix=body.mix, buses=body.buses, stems=body.stems,
                                   midi=body.midi, midi_stems=body.midi_stems,
                                   bit_depth=body.bit_depth, range=body.range,
                                   cache=StemStore(proj.root))
        except ValueError as e:   # E6 段2：位深/选段参数非法 → 400（契约 §五 {"error": ...}）
            raise HTTPException(400, str(e))
        finally:
            session.close()
        return report

    # ---------------- P62b：插件编辑器状态（前端轮询用） ----------------

    @app.get("/api/projects/{name}/instrument/editor-status")
    def instrument_editor_status(name: str) -> dict:
        """插件编辑器窗口状态（worker 级；轻量、不拉起 worker）。"""
        st().get_project(name)   # 工程存在性校验（与其余端点 404 语义一致）
        from ...host.vst_worker import editor_status

        return editor_status()
