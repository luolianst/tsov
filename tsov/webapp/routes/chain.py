"""处理链域路由（M-V8 E3 段2/3）。

- run / status / config / cancel / artifact / tools / apply（段3：进工程·处理版单轨）
- 运行器：每工程一个 ChainRunner（app.state.tsov.chains）；一次一个 run（409）
- chain.json 工程伴生（参数/mute/源轨）；产物 <project>/chain/<run-ts>/
- SSE：chain_started / chain_step / chain_log / chain_finished（轮询为主的加成）
"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException

from ...chain import (ChainRunner, apply_config, auto_run_plan, build_apply_commands,
                      build_decompose_commands, list_presets, load_chain_json,
                      load_decompose_artifacts, load_preset, load_run_notes, list_tools)
from ...host import EditBatch
from ..helpers import project_state
from ..state import WebState

DEFAULT_PRESET = "humming-quicklane"


def apply_chain_run(ws: WebState, proj, *, run_ts=None, source_track=None) -> dict:
    """链产物进工程——/chain/apply 与暂存区采纳共用同一动作路径（ADR-0017）。

    - 预设 song-decompose → 拆解落位（stems 四路入库＋文件夹＋人声 MIDI；E1-5）
    - 其余（哼唱）→ 单轨·处理版：最终音符 → 源轨正下方「<源轨名> · 处理」MIDI 轨。
    幂等：两路皆先清旧产物再重建。run_ts 缺省 = chain.json 最近一次；source_track 缺省同。
    """
    saved = load_chain_json(proj.root)
    preset_id = str(saved.get("preset") or DEFAULT_PRESET)
    if preset_id == "song-decompose":
        return _apply_decompose_run(ws, proj, saved=saved, run_ts=run_ts,
                                    source_track=source_track)
    run_ts = str(run_ts or saved.get("run_ts") or "")
    if not run_ts:
        raise HTTPException(409, "该工程还没有链产物（先运行一次链）")
    run_dir = proj.root / "chain" / run_ts
    got = load_run_notes(run_dir)
    if not got["processed"]:
        raise HTTPException(409, f"运行 {run_ts} 缺处理版产物（05-notes-snapped.json）——先跑完转录与处理步")
    if source_track is None:
        source_track = saved.get("source_track")
    if source_track is None:
        raise HTTPException(409, "链未记录源轨（先运行一次链或显式给 source_track）")
    try:
        plan = build_apply_commands(list(proj.score.tracks), source_track=int(source_track),
                                    notes=got["processed"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    batch = EditBatch(label="转谱进工程（处理版）")
    for c in plan["commands"]:
        batch.add(c["op"], track=int(c.get("track", 0)), value=c.get("value"))
    result = proj.apply_batch(batch, commit_message="转谱进工程：处理版（量化+调内吸附）",
                              source="user")
    if result["applied"] == 0:
        return {"ok": False, "project": proj.name, **plan["meta"], **result}
    ws.bus.publish(proj.name, "diff_applied", {**result["diff"], "commit": result["commit"],
                                               "seq": result.get("seq")})
    ws.bus.publish(proj.name, "state_updated", project_state(proj))
    return {"ok": True, "project": proj.name, "run_ts": run_ts, **plan["meta"],
            "applied": result["applied"], "errors": result.get("errors") or [],
            "tracks": len(proj.score.tracks)}


def _apply_decompose_run(ws: WebState, proj, *, saved: dict, run_ts=None,
                         source_track=None) -> dict:
    """拆解落位（E1-5）：四 stems 入库 → 文件夹「拆解 · <源轨名>」＋4 音频轨＋人声 MIDI 轨。

    grid.json 在且 bpm 有效 → 命令层 set_tempo（remap=false，音符秒值不动；读回对拍 applied）；
    失败不阻断落位（meta 注记）。幂等：文件夹级清旧重建（源轨除外）。
    """
    run_ts = str(run_ts or saved.get("run_ts") or "")
    if not run_ts:
        raise HTTPException(409, "该工程还没有链产物（先运行一次链）")
    run_dir = proj.root / "chain" / run_ts
    art = load_decompose_artifacts(run_dir)
    missing = [s for s, rel in art["stems"].items() if not rel]
    if missing:
        raise HTTPException(409, f"运行 {run_ts} 缺分离产物："
                                 f"{'、'.join(f'stems/{s}.wav' for s in missing)}——先跑完「分离」步")
    if art["vocals_notes"] is None:
        raise HTTPException(409, f"运行 {run_ts} 缺人声转录产物（voice-vocals.json 系）"
                                 "——先跑完转录/处理步")
    if source_track is None:
        source_track = saved.get("source_track")
    if source_track is None:
        raise HTTPException(409, "链未记录源轨（先运行一次链或显式给 source_track）")

    # ① 四路入库（内容寻址；重复入库自动去重）
    stem_files: dict = {}
    try:
        for s, rel in art["stems"].items():
            info = proj.import_audio(run_dir / rel)
            stem_files[s] = info["file"]
    except ValueError as e:
        raise HTTPException(400, f"分离轨入库失败：{e}") from e

    # ② 组装命令（含幂等清旧）
    midis = [{"stem": "vocals", "notes": art["vocals_notes"]}]
    try:
        plan = build_decompose_commands(list(proj.score.tracks), source_track=int(source_track),
                                        stems=stem_files, midis=midis)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # ③ 写 tempo（grid.json 在且 bpm 有效 → set_tempo；失败不阻断落位）
    tempo_meta = {"from": float(proj.score.tempo or 120.0), "to": None, "applied": False}
    grid = run_dir / "grid.json"
    if grid.is_file():
        try:
            g = json.loads(grid.read_text(encoding="utf-8"))
            bpm = float(g.get("bpm") or 0)
        except (OSError, ValueError):
            bpm = 0.0
        if 20.0 <= bpm <= 400.0:
            tempo_meta["to"] = round(bpm, 3)
            plan["commands"].append({"op": "set_tempo", "value": {"tempo": tempo_meta["to"]}})

    batch = EditBatch(label="拆解落位")
    for c in plan["commands"]:
        batch.add(c["op"], track=int(c.get("track", 0)), value=c.get("value"))
    result = proj.apply_batch(batch, commit_message="拆解落位：分离轨 + 人声 MIDI",
                              source="user")
    if result["applied"] == 0:
        return {"ok": False, "project": proj.name, "run_ts": run_ts, **plan["meta"],
                "tempo": tempo_meta, **result}
    tempo_meta["applied"] = bool(tempo_meta["to"] is not None
                                 and abs(float(proj.score.tempo or 0) - tempo_meta["to"]) < 1e-6)
    ws.bus.publish(proj.name, "diff_applied", {**result["diff"], "commit": result["commit"],
                                               "seq": result.get("seq")})
    ws.bus.publish(proj.name, "state_updated", project_state(proj))
    return {"ok": True, "project": proj.name, "run_ts": run_ts, **plan["meta"],
            "tempo": tempo_meta, "notes_source": art["notes_source"],
            "applied": result["applied"], "errors": result.get("errors") or [],
            "tracks": len(proj.score.tracks)}


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    def _tool_map() -> dict:
        return {t["id"]: t for t in list_tools()}

    def _static_steps(saved: dict) -> tuple[list[dict], dict]:
        """无活跃 runner 时的静态槽位（chain.json 状态；无则预设 defaults，全部 pending）。"""
        tools = _tool_map()
        preset_id = str(saved.get("preset") or DEFAULT_PRESET)
        try:
            preset = load_preset(preset_id)
        except ValueError:
            preset = load_preset(DEFAULT_PRESET)
            preset_id = DEFAULT_PRESET
        saved_steps = {s.get("tool"): s for s in (saved.get("steps") or [])}
        steps: list[dict] = []
        for entry in preset["steps"]:
            tid = str(entry.get("tool") or "")
            tool = tools.get(tid)
            if tool is None:
                continue
            old = saved_steps.get(tid) or {}
            params = {k: v.get("default") for k, v in tool["params"].items()}
            params.update(entry.get("params") or {})
            params.update(old.get("params") or {})
            steps.append({
                "tool_id": tid, "name": tool["name"], "tier": tool["tier"],
                "params": params, "mute": bool(old.get("mute", False)),
                "status": str(old.get("status") or "pending"),
                "error": old.get("error"), "artifact": old.get("artifact"),
                "stats": old.get("stats") or {}, "elapsed_sec": old.get("elapsed_sec"),
            })
        meta = {"preset": preset_id, "source_track": saved.get("source_track"),
                "run_ts": saved.get("run_ts")}
        return steps, meta

    def _get_runner(name: str) -> ChainRunner | None:
        with st().chains_lock:
            return st().chains.get(name)

    # ---------------- 链 ----------------

    @app.get("/api/projects/{name}/chain/tools")
    def chain_tools(name: str) -> dict:
        """工具注册表（前端渲染参数表单/槽位名）＋预设清单（E1-6 面板选择器）。

        preset＝chain.json 当前预设（缺省默认）——面板进入即回读（10-12 修复，曾硬编码默认）。
        """
        proj = st().get_project(name)
        saved = load_chain_json(proj.root)
        return {"tools": list_tools(), "presets": list_presets(),
                "preset": str(saved.get("preset") or DEFAULT_PRESET)}

    @app.post("/api/projects/{name}/chain/run")
    def chain_run(name: str, body: dict | None = None) -> dict:
        """挂链并开跑：{preset?, source_track?, overrides?, steps?[工具id]}。"""
        body = body or {}
        cur = _get_runner(name)
        if cur is not None and cur.running:
            raise HTTPException(409, "链正在运行中（先取消或等完成）")
        try:
            proj = st().get_project(name)
            saved = load_chain_json(proj.root)
            runner = ChainRunner(
                proj,
                # 预设优先级：显式 body > chain.json 当前预设 > 默认
                #（10-12 修复：面板切换预设后「▶ 运行」不带头跑，曾丢回默认哼唱）
                preset_id=str(body.get("preset") or saved.get("preset") or DEFAULT_PRESET),
                source_track=body.get("source_track"),
                overrides=body.get("overrides") or {},
            )
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from e
        runner.on_event = lambda ev, data: st().bus.publish(name, ev, {"project": name, **data})
        with st().chains_lock:
            st().chains[name] = runner
        try:
            snap = runner.start(steps=body.get("steps"))
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "project": name, **snap}

    @app.get("/api/projects/{name}/chain/status")
    def chain_status(name: str) -> dict:
        """链状态（前端轮询）：活跃 runner 快照；无 → chain.json/预设静态槽位。"""
        proj = st().get_project(name)
        r = _get_runner(name)
        if r is not None:
            return r.snapshot()
        saved = load_chain_json(proj.root)
        steps, meta = _static_steps(saved)
        done = sum(1 for s in steps if s["status"] in ("done", "skipped"))
        return {"running": False, "saved_only": True, "error": None, "finished": False,
                "progress": {"done": done, "total": len(steps)}, "steps": steps, **meta}

    @app.post("/api/projects/{name}/chain/config")
    def chain_config(name: str, body: dict | None = None) -> dict:
        """改 mute/参数/源轨（存 chain.json，不跑）；改参返回顺跑计划（Q2）。

        - 快档改参：{"auto": [工具…]}（前端据此自动触发 run steps 子集）
        - 慢档改参：{"needs_apply": 工具}（前端亮「应用」按钮）
        """
        body = body or {}
        proj = st().get_project(name)
        try:
            data = apply_config(proj, mute=body.get("mute"), overrides=body.get("overrides"),
                                source_track=body.get("source_track"), preset_id=body.get("preset"))
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from e
        resp = {"ok": True, "config": data}
        overrides = body.get("overrides") or {}
        if overrides:
            tid = next(iter(overrides))
            try:
                resp["plan"] = auto_run_plan(proj, tid, preset_id=str(data.get("preset") or DEFAULT_PRESET))
            except (ValueError, KeyError) as e:
                raise HTTPException(400, str(e)) from e
        # 运行中的 runner：同步 mute/参数到当前 run 的后续步（不打断当前步）
        r = _get_runner(name)
        if r is not None and not r.running:
            with st().chains_lock:
                st().chains.pop(name, None)  # 参数已变 → 丢弃旧快照，下次 status 读 chain.json
        return resp

    @app.post("/api/projects/{name}/chain/cancel")
    def chain_cancel(name: str) -> dict:
        """取消进行中的链（步边界生效；已完成产物保留）。"""
        st().get_project(name)
        r = _get_runner(name)
        if r is None or not r.running:
            raise HTTPException(409, "没有进行中的链运行")
        return {"ok": True, "project": name, **r.cancel()}

    @app.post("/api/projects/{name}/chain/apply")
    def chain_apply(name: str, body: dict | None = None) -> dict:
        """链产物进工程（单轨·处理版）：最终音符 → 源轨正下方一条「<源轨名> · 处理」MIDI 轨（命令层事务）。

        - 幂等：重跑先删同名的旧产物轨再插入（含旧版双轨遗留）；undo 可回（快照窗口）
        - body: {run_ts?（缺省最近一次）、source_track?（缺省 chain.json 记录）}
        """
        body = body or {}
        proj = st().get_project(name)
        return apply_chain_run(st(), proj, run_ts=body.get("run_ts"),
                               source_track=body.get("source_track"))

    @app.get("/api/projects/{name}/chain/artifact")
    def chain_artifact(name: str, file: str):
        """链产物试听/下载（限最近一次 run 目录内，防穿越）。"""
        from fastapi.responses import FileResponse

        proj = st().get_project(name)
        saved = load_chain_json(proj.root)
        run_ts = str(saved.get("run_ts") or "")
        if not run_ts:
            raise HTTPException(404, "该工程还没有链产物（先运行一次）")
        base = (proj.root / "chain" / run_ts).resolve()
        path = (base / file).resolve()
        if path != base and base not in path.parents:
            raise HTTPException(400, f"非法产物路径：{file!r}")
        if not path.is_file():
            raise HTTPException(404, f"产物不存在：{file}")
        suffix = path.suffix.lower()
        media = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg",
                 "mid": "audio/midi", "json": "application/json"}.get(suffix.lstrip("."),
                                                                       "application/octet-stream")
        return FileResponse(str(path), media_type=media, filename=path.name)
