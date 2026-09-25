"""处理链域路由（M-V8 E3 段2）。

- run / status / config / cancel / artifact / tools
- 运行器：每工程一个 ChainRunner（app.state.tsov.chains）；一次一个 run（409）
- chain.json 工程伴生（参数/mute/源轨）；产物 <project>/chain/<run-ts>/
- SSE：chain_started / chain_step / chain_log / chain_finished（轮询为主的加成）
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ...chain import ChainRunner, apply_config, auto_run_plan, load_chain_json, load_preset, list_tools
from ..state import WebState

DEFAULT_PRESET = "humming-quicklane"


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
        """工具注册表（前端渲染参数表单/槽位名）。"""
        st().get_project(name)
        return {"tools": list_tools(), "preset": DEFAULT_PRESET}

    @app.post("/api/projects/{name}/chain/run")
    def chain_run(name: str, body: dict | None = None) -> dict:
        """挂链并开跑：{preset?, source_track?, overrides?, steps?[工具id]}。"""
        body = body or {}
        cur = _get_runner(name)
        if cur is not None and cur.running:
            raise HTTPException(409, "链正在运行中（先取消或等完成）")
        try:
            runner = ChainRunner(
                st().get_project(name),
                preset_id=str(body.get("preset") or DEFAULT_PRESET),
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
