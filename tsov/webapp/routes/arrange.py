"""配器域路由（M-V8 E4 段3 · Q7/Q14）：generate / list / get / preview / apply / discard / file。

- generate：事实 → 决策（LLM 只选 ID）→ 展开 → 批次 <工程>/arrange/<ts>.json（暂存区 producer=arrange）
- preview：候选混音（当前工程 + 配器轨副本）渲染试听——工程零改动
- apply：轨规格 → 命令层事务 EditBatch（幂等：重跑先删同名旧产物轨）→ 记账 + 暂存处置
- SSE：diff_applied / state_updated / arrange_updated
- 上下文装配（Q15 双层）：复用 tune 域 _context_text（工程 agents.md + 全局 agents-user.md）
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ... import staging
from ...arrange import store
from ...arrange.apply import build_arrange_commands
from ...arrange.generate import candidate_score, generate_batch
from ...host import EditBatch
from ...host.cache import StemStore
from ..helpers import project_state
from ..state import WebState
from .tune import _context_text

_FILE_MEDIA = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg",
               "mid": "audio/midi", "json": "application/json"}


def _render_mix(engine, proj, score, out: Path, cache=None) -> None:
    """渲染混音到 wav（试听预览用；同 tune 域口径）。"""
    session = engine.load(score, base_dir=proj.root)
    try:
        engine.render(session, out_wav=str(out), stereo=True,
                      cache=cache or StemStore(proj.root))
    finally:
        session.close()


def _resolve_anchor(score, batch: dict) -> int:
    """锚轨（配器插它下面）：按录音时的旋律轨名找；找不到退索引；再退末尾。"""
    mel = (batch.get("facts") or {}).get("melody") or {}
    name = str(mel.get("name") or "")
    tracks = list(getattr(score, "tracks", []) or [])
    if name:
        for i, t in enumerate(tracks):
            if str(getattr(t, "name", "") or "") == name:
                return i
    idx = mel.get("track")
    if isinstance(idx, int) and 0 <= idx < len(tracks):
        return int(idx)
    return max(0, len(tracks) - 1)


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    def _get_batch_or_404(proj, ts) -> dict:
        try:
            return store.load_batch(proj.root, ts)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ---------------- 生成 / 列表 ----------------

    @app.post("/api/projects/{name}/arrange/generate")
    def arrange_generate(name: str, body: dict | None = None) -> dict:
        """{pack, strength?, melody_track?, context?} → 配器批次（暂存区 producer=arrange）。"""
        body = body or {}
        proj = st().get_project(name)
        pack = str(body.get("pack") or "").strip()
        if not pack:
            raise HTTPException(400, "需要 pack（风格包名；可选见 presets/arrangements/*.patterns.json）")
        strength = str(body.get("strength") or "standard")
        context = str(body.get("context") or "") or _context_text(proj)
        try:
            batch = generate_batch(proj.root, proj.score, pack=pack, strength=strength,
                                   melody_track=body.get("melody_track"), name=proj.name,
                                   context_md=context)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        st().bus.publish(name, "arrange_updated",
                         {"batch_ts": batch["batch_ts"], "state": "pending",
                          "n": batch["stats"]["tracks"]})
        return {"ok": True, "project": name, "batch": batch}

    @app.get("/api/projects/{name}/arrange/packs")
    def arrange_packs(name: str) -> dict:
        """可用风格包（有模式库者）+ 强度档。"""
        from ...arrange.library import available_packs, load_patterns

        st().get_project(name)
        packs = []
        for p in available_packs():
            try:
                lib = load_patterns(p)
            except (ValueError, OSError):
                continue
            packs.append({"pack": p, "title": lib.description[:80] or p,
                          "meter": lib.meter, "roles": len(lib.roles),
                          "strengths": sorted(lib.strengths)})
        return {"ok": True, "project": name, "packs": packs}

    @app.get("/api/projects/{name}/arrange/list")
    def arrange_list(name: str) -> dict:
        proj = st().get_project(name)
        return {"ok": True, "project": name, "batches": store.list_batches(proj.root)}

    @app.get("/api/projects/{name}/arrange/{batch_ts}")
    def arrange_get(name: str, batch_ts: str) -> dict:
        proj = st().get_project(name)
        return {"ok": True, "project": name, "batch": _get_batch_or_404(proj, batch_ts)}

    # ---------------- 试听 / 进工程 / 丢弃 ----------------

    @app.post("/api/projects/{name}/arrange/preview")
    def arrange_preview(name: str, body: dict | None = None) -> dict:
        """候选混音试听（当前工程 + 配器轨副本；工程零改动）。body: {batch_ts}。"""
        body = body or {}
        proj = st().get_project(name)
        ts = str(body.get("batch_ts") or "")
        batch = _get_batch_or_404(proj, ts)
        specs = batch.get("tracks") or []
        if not specs:
            raise HTTPException(400, "该批次没有可试听的轨")
        cand = candidate_score(proj.score, specs)
        out = store.batch_dir(proj.root, ts) / "preview-mix.wav"
        _render_mix(st().engine(), proj, cand, out)
        store.append_event(proj.root, "preview", {"batch_ts": ts, "file": out.name})
        return {"ok": True, "project": name, "batch_ts": ts, "file": out.name,
                "url": f"/api/projects/{name}/arrange/{ts}/file?file={out.name}"}

    @app.post("/api/projects/{name}/arrange/apply")
    def arrange_apply(name: str, body: dict | None = None) -> dict:
        """配器进工程（命令层事务，幂等）。body: {batch_ts, anchor_track?}。"""
        body = body or {}
        proj = st().get_project(name)
        ts = str(body.get("batch_ts") or "")
        batch = _get_batch_or_404(proj, ts)
        specs = batch.get("tracks") or []
        if not specs:
            raise HTTPException(400, "该批次没有可落地的轨")
        if body.get("anchor_track") is not None:
            try:
                anchor = int(body["anchor_track"])
            except (TypeError, ValueError):
                raise HTTPException(400, f"anchor_track 非法：{body.get('anchor_track')!r}") from None
        else:
            anchor = _resolve_anchor(proj.score, batch)
        try:
            plan = build_arrange_commands(list(proj.score.tracks), specs=specs, anchor_track=anchor)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        eb = EditBatch(label=f"配器进工程（{ts}）")
        for c in plan["commands"]:
            eb.add(str(c["op"]), track=int(c.get("track", 0)), value=c.get("value"))
        result = proj.apply_batch(eb, commit_message=f"配器进工程：{ts}（{plan['meta']['n_tracks']} 轨）",
                                  source="user")
        if result.get("applied", 0) == 0:
            raise HTTPException(409, "全部命令被拒：" + "；".join(result.get("errors") or [])[:400])
        st().bus.publish(name, "diff_applied", {**(result.get("diff") or {}),
                                                "commit": result.get("commit"),
                                                "seq": result.get("seq")})
        st().bus.publish(name, "state_updated", project_state(proj))

        got = store.update_batch(proj.root, ts, state="adopted",
                                 applied=[t["name"] for t in plan["meta"]["tracks"]],
                                 decided_at=time.time())
        try:
            staging.set_state(proj.root, f"arrange:{ts}", "adopted",
                              note=f"落地 {plan['meta']['n_tracks']} 轨 / {plan['meta']['n_notes']} 音")
        except ValueError:
            pass
        store.append_event(proj.root, "apply",
                           {"batch_ts": ts, "tracks": plan["meta"]["n_tracks"],
                            "notes": plan["meta"]["n_notes"], "anchor": anchor,
                            "applied": result.get("applied"), "errors": result.get("errors") or []})
        st().bus.publish(name, "arrange_updated", {"batch_ts": ts, "state": "adopted",
                                                   "n": plan["meta"]["n_tracks"]})
        return {"ok": True, "project": name, "batch_ts": ts, **plan["meta"],
                "applied": result.get("applied"), "errors": result.get("errors") or [],
                "tracks_total": len(proj.score.tracks), "state": got.get("state")}

    @app.post("/api/projects/{name}/arrange/discard")
    def arrange_discard(name: str, body: dict | None = None) -> dict:
        """丢弃批次（仅记处置：不动工程、不删文件）。body: {batch_ts}。"""
        body = body or {}
        proj = st().get_project(name)
        ts = str(body.get("batch_ts") or "")
        _get_batch_or_404(proj, ts)
        rec = staging.set_state(proj.root, f"arrange:{ts}", "discarded")
        store.update_batch(proj.root, ts, state="discarded", decided_at=time.time())
        store.append_event(proj.root, "discard", {"batch_ts": ts})
        st().bus.publish(name, "arrange_updated", {"batch_ts": ts, "state": "discarded", "n": 0})
        return {"ok": True, "project": name, "batch_ts": ts, "record": rec}

    @app.get("/api/projects/{name}/arrange/{batch_ts}/file")
    def arrange_file(name: str, batch_ts: str, file: str):
        """批次音频（preview-mix.wav）试听；限批次目录内，防穿越。"""
        proj = st().get_project(name)
        _get_batch_or_404(proj, batch_ts)
        base = store.batch_dir(proj.root, batch_ts).resolve()
        path = (base / file).resolve()
        if path != base and base not in path.parents:
            raise HTTPException(400, f"非法产物路径：{file!r}")
        if not path.is_file():
            raise HTTPException(404, f"文件不存在：{file}")
        media = _FILE_MEDIA.get(path.suffix.lower().lstrip("."), "application/octet-stream")
        return FileResponse(str(path), media_type=media, filename=path.name)
