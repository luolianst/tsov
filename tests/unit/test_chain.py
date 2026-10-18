"""M-V8 E3 段2 单测：处理链——工具 / 运行器 / chain.json / REST。

- 工具层：注册表形态；transcribe 打桩；quantize 真跑；denoise/loudnorm 真跑（skipif ffmpeg）
- 运行器：全链 mock 跑通（状态机/产物/chain.json）；mute→skipped；取消（步边界）；
  失败停步；源轨守卫
- 计划/配置：auto_run_plan（快档顺跑 / 慢档停）；apply_config 持久化；config 端点
- REST：静态 5 槽位；run→轮询；cancel 409；artifact 守卫（200/400/404）
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

import tsov.web
from tsov.chain import (ChainRunner, apply_config, auto_run_plan, build_apply_commands,
                        build_decompose_commands, list_presets, list_tools, load_chain_json,
                        load_preset, load_run_notes)
from tsov.chain.tools import TOOLS, ChainContext, get_tool
from tsov.web import create_app

from unit._cleanup import rmtree_force

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
_ORDER = ["analyze_grid", "separate", "denoise", "loudnorm", "transcribe", "quantize",
          "snap_scale"]                                                                     # 工具全集序
_HUMMING = ["denoise", "loudnorm", "transcribe", "quantize", "snap_scale"]               # 哼唱链预设序


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"chaintest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        rmtree_force(d)


def _make_project(env, name="p1"):
    r = env["client"].post("/api/projects", json={
        "name": name,
        "score": {
            "title": "t",
            "tempo": 120.0,
            "key_candidates": [{"key": "C major", "confidence": 0.7}],
            "tracks": [
                {"name": "melody",
                 "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
                 "notes": []}
            ],
            "meta": {},
        }})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text


def _write_wav(path, seconds=0.4, sr=16000):
    t = np.arange(int(seconds * sr)) / sr
    sig = (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), sig, sr)


def _project_with_audio(env, name="p1"):
    """工程 + 导入音频 + 命令层加音频轨（source_track 自动=该轨）。"""
    _make_project(env, name)
    src = env["dir"] / f"{name}-hum.wav"
    _write_wav(src)
    info = env["client"].post(f"/api/projects/{name}/audio/import", json={"path": str(src)}).json()
    r = env["client"].post(f"/api/projects/{name}/batch", json={
        "label": "导入音频",
        "commands": [{"op": "add_audio_track", "value": {"file": info["file"]}}]})
    assert r.status_code == 200 and r.json()["applied"] == 1, r.text


def _patch_tools(monkeypatch):
    """5 工具 run → 假实现（写产物文件），记录调用序。"""
    calls: list[str] = []

    def factory(tid):
        def run(ctx):
            calls.append(tid)
            ctx.check_cancel()
            (ctx.artifact(f"{tid}.out")).write_text("x", encoding="utf-8")
            return {"artifact": f"{tid}.out"}
        return run

    for _tid, tool in TOOLS.items():
        monkeypatch.setitem(tool, "run", factory(_tid))
    return calls


def _proj(env, name="p1"):
    return env["app"].state.tsov.get_project(name)


def _ctx(proj_root, run_dir, src, step_id, params=None, prev=None):
    return ChainContext(project_root=proj_root, run_dir=run_dir, source_audio=src,
                        score_tempo=120.0, step_id=step_id, params=params or {},
                        prev=prev, cancel=threading.Event())


def _wait_runner(runner, timeout=25):
    t0 = time.time()
    while runner.running and time.time() - t0 < timeout:
        time.sleep(0.05)
    assert not runner.running, "运行器未在超时内结束"
    return runner.snapshot()


def _wait_http(client, name, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = client.get(f"/api/projects/{name}/chain/status").json()
        if not st["running"]:
            return st
        time.sleep(0.1)
    raise AssertionError("链未在超时内结束")


# ---------------- 工具层 ----------------


def test_tool_registry_shape():
    tools = list_tools()
    assert [t["id"] for t in tools] == _ORDER
    assert {t["id"]: t["tier"] for t in tools} == {
        "analyze_grid": "fast", "separate": "slow",
        "denoise": "fast", "loudnorm": "fast", "transcribe": "slow",
        "quantize": "fast", "snap_scale": "fast"}
    for t in tools:
        assert callable(TOOLS[t["id"]]["run"]) and t["name"]["zh"] and t["params"]
        assert "run" not in t
    assert [s["tool"] for s in load_preset("humming-quicklane")["steps"]] == _HUMMING
    with pytest.raises(ValueError):
        load_preset("nope-preset")


def test_transcribe_tool_stubbed(tmp_path, monkeypatch):
    class FakeVoice:
        bpm = 120.0
        notes: list = []

        def to_dict(self):
            return {"bpm": self.bpm, "notes": []}

    seen: dict = {}

    def fake_transcribe(path, backend="rmvpe", **kw):
        seen.update({"path": str(path), "backend": backend, **kw})
        return FakeVoice()

    import importlib

    mod = importlib.import_module("tsov.dsp.transcribe")
    monkeypatch.setattr(mod, "transcribe", fake_transcribe)

    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.flac"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    ctx = _ctx(proj_root, run_dir, src, "transcribe",
               {"backend": "rmvpe", "confidence_threshold": 0.5, "min_note_ms": 80.0})
    out = get_tool("transcribe")["run"](ctx)
    assert out["notes"] == 0 and out["backend"] == "rmvpe"
    assert seen["backend"] == "rmvpe" and seen["confidence_threshold"] == 0.5 and seen["min_note_ms"] == 80.0
    data = json.loads((run_dir / out["artifact"]).read_text(encoding="utf-8"))
    assert data == {"bpm": 120.0, "notes": []}


def test_quantize_tool_real(tmp_path):
    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.flac"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    voice = {"bpm": 120.0, "notes": [
        {"start": 0.03, "end": 0.26, "pitch_midi": 60, "pitch_hz": 261.6, "deviation_cents": 0.0},
        {"start": 0.51, "end": 0.74, "pitch_midi": 62, "pitch_hz": 293.7, "deviation_cents": 0.0},
    ]}
    (run_dir / "03-voice.json").write_text(json.dumps(voice), encoding="utf-8")
    ctx = _ctx(proj_root, run_dir, src, "quantize", {"grid": 16, "strength": 1.0},
               prev=run_dir / "03-voice.json")
    out = get_tool("quantize")["run"](ctx)
    q = json.loads((run_dir / out["artifact"]).read_text(encoding="utf-8"))
    # quantize_notes：cell = (60/120)/16 = 0.03125s：0.03→0.03125、0.51→0.5
    cell = 0.03125
    assert all(abs(n["start"] % cell) < 1e-9 for n in q)
    assert q[0]["start"] == pytest.approx(cell, abs=1e-9)
    assert q[1]["start"] == pytest.approx(0.5, abs=1e-9)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_denoise_loudnorm_tools_real(tmp_path):
    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.wav"
    _write_wav(src, seconds=0.5, sr=16000)
    ctx = _ctx(proj_root, run_dir, src, "denoise", {"strength": 0.5})
    out = get_tool("denoise")["run"](ctx)
    art = run_dir / out["artifact"]
    assert art.is_file() and art.stat().st_size > 1000
    assert (run_dir / "00-source-16k.wav").is_file()
    ctx2 = _ctx(proj_root, run_dir, src, "loudnorm", {"target_lufs": -18.0}, prev=art)
    out2 = get_tool("loudnorm")["run"](ctx2)
    assert (run_dir / out2["artifact"]).is_file() and out2["target_lufs"] == -18.0


# ---------------- 运行器 ----------------


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_full_mocked(env, monkeypatch):
    _project_with_audio(env)
    calls = _patch_tools(monkeypatch)
    r = ChainRunner(_proj(env))
    r.start()
    st = _wait_runner(r)
    assert calls == _HUMMING
    assert [s["status"] for s in st["steps"]] == ["done"] * 5
    saved = load_chain_json(env["dir"] / "p1")
    assert saved["run_ts"] and [s["tool"] for s in saved["steps"]] == _HUMMING
    run_dir = env["dir"] / "p1" / "chain" / saved["run_ts"]
    assert (run_dir / "denoise.out").is_file() and (run_dir / "snap_scale.out").is_file()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_mute_skips(env, monkeypatch):
    _project_with_audio(env)
    calls = _patch_tools(monkeypatch)
    proj = _proj(env)
    apply_config(proj, mute={"quantize": True})
    r = ChainRunner(proj)
    r.start()
    st = _wait_runner(r)
    assert calls == ["denoise", "loudnorm", "transcribe", "snap_scale"]
    assert [s["status"] for s in st["steps"]] == ["done", "done", "done", "skipped", "done"]


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_cancel_at_boundary(env, monkeypatch):
    _project_with_audio(env)
    _patch_tools(monkeypatch)

    def cancelling(ctx):
        ctx.check_cancel()
        ctx.artifact("a.out").write_text("x", encoding="utf-8")
        ctx.cancel.set()
        return {"artifact": "a.out"}

    monkeypatch.setitem(TOOLS["denoise"], "run", cancelling)
    r = ChainRunner(_proj(env))
    r.start()
    st = _wait_runner(r)
    assert [s["status"] for s in st["steps"]] == ["done", "cancelled", "pending", "pending", "pending"]


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_failure_stops(env, monkeypatch):
    _project_with_audio(env)
    _patch_tools(monkeypatch)

    def bad(ctx):
        raise RuntimeError("boom")

    monkeypatch.setitem(TOOLS["loudnorm"], "run", bad)
    r = ChainRunner(_proj(env))
    r.start()
    st = _wait_runner(r)
    statuses = [s["status"] for s in st["steps"]]
    assert statuses[0] == "done" and statuses[1] == "failed"
    assert statuses[2:] == ["pending", "pending", "pending"]
    assert "boom" in (st["steps"][1]["error"] or "")
    assert st["error"] is None  # 步级失败不升级为运行器级错误


def test_runner_source_guard(env):
    _make_project(env)  # 无音频轨
    with pytest.raises(ValueError):
        ChainRunner(_proj(env))


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_source_clip_form(env):
    """E6 clip 形态兼容：拖动/切片后的音轨仍可作链源（单片段取引用；多片段明确报错）。"""
    _project_with_audio(env)
    proj = _proj(env)
    f = proj.score.tracks[1].audio["file"]

    def set_clips(clips):
        return env["client"].post("/api/projects/p1/batch", json={
            "label": "移动音频片段",
            "commands": [{"op": "set_audio_clips", "track": 1, "value": {"clips": clips}}]})

    one = [{"file": f, "start": 0.6, "src_offset": 0.0, "src_len": None, "stretch": 1.0,
            "fade_in": 0.0, "fade_out": 0.0}]
    r = set_clips(one)
    assert r.status_code == 200 and r.json()["applied"] == 1, r.text
    p = ChainRunner(_proj(env)).source_audio_path()
    assert p.is_file() and p.name.endswith(".flac")

    two = one + [{"file": f, "start": 2.0, "src_offset": 0.0, "src_len": None, "stretch": 1.0,
                  "fade_in": 0.0, "fade_out": 0.0}]
    r = set_clips(two)
    assert r.status_code == 200 and r.json()["applied"] == 1, r.text
    with pytest.raises(ValueError, match="多片段"):
        ChainRunner(_proj(env)).source_audio_path()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_status_restore_from_chain_json(env, monkeypatch):
    """新 runner（模拟 web 重启 / 重新挂链）应恢复 chain.json 的上轮完成态与产物。"""
    _project_with_audio(env)
    _patch_tools(monkeypatch)
    proj = _proj(env)
    r = ChainRunner(proj)
    r.start()
    st = _wait_runner(r)
    assert [s["status"] for s in st["steps"]] == ["done"] * 5
    r2 = ChainRunner(proj)
    snap = r2.snapshot()
    assert [s["status"] for s in snap["steps"]] == ["done"] * 5
    assert snap["run_ts"] == st["run_ts"]
    assert snap["steps"][0]["artifact"] == "denoise.out"
    assert snap["steps"][0]["stats"] == {"artifact": "denoise.out"}


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_subset_inherits_prev_round_artifact(env, monkeypatch):
    """子集 run：上游步被跳过/不在 targets → 输入回退到「上一轮」对应步产物（重试/跳过语义）。"""
    _project_with_audio(env)
    _patch_tools(monkeypatch)
    proj = _proj(env)
    r = ChainRunner(proj)
    r.start()
    st1 = _wait_runner(r)
    run_ts1 = st1["run_ts"]
    assert run_ts1
    seen: dict = {}

    def snap_fake(ctx):
        seen["prev"] = str(ctx.prev) if ctx.prev else None
        ctx.check_cancel()
        (ctx.artifact("snap_scale.out")).write_text("x", encoding="utf-8")
        return {"artifact": "snap_scale.out"}

    monkeypatch.setitem(TOOLS["snap_scale"], "run", snap_fake)
    apply_config(proj, mute={"quantize": True})
    r2 = ChainRunner(proj)
    r2.start(steps=["quantize", "snap_scale"])
    st2 = _wait_runner(r2)
    sts = {s["tool_id"]: s for s in st2["steps"]}
    assert sts["quantize"]["status"] == "skipped"
    assert sts["snap_scale"]["status"] == "done"
    # snap 的输入 = 上一轮 quantize 的产物（run_ts1 目录下），而非本轮（quantize 未跑）
    assert seen["prev"], seen
    assert run_ts1 in seen["prev"] and seen["prev"].endswith("quantize.out"), seen


# ---------------- 配置 / 计划 ----------------


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_auto_run_plan_and_config(env):
    _project_with_audio(env)
    proj = _proj(env)
    assert auto_run_plan(proj, "denoise") == {"auto": [0, 1], "stops_at": 2}
    assert auto_run_plan(proj, "transcribe") == {"needs_apply": 2}
    assert auto_run_plan(proj, "quantize") == {"auto": [3, 4]}
    apply_config(proj, mute={"quantize": True}, overrides={"denoise": {"strength": 0.2}},
                 source_track=0)
    saved = load_chain_json(env["dir"] / "p1")
    assert saved["source_track"] == 0
    d = {s["tool"]: s for s in saved["steps"]}
    assert d["quantize"]["mute"] is True and d["denoise"]["params"]["strength"] == 0.2
    with pytest.raises(ValueError):
        auto_run_plan(proj, "nope")


def test_chain_config_endpoint(env):
    _make_project(env)
    r = env["client"].post("/api/projects/p1/chain/config",
                           json={"overrides": {"denoise": {"strength": 0.3}}})
    assert r.status_code == 200
    assert r.json()["plan"] == {"auto": [0, 1], "stops_at": 2}
    d = {s["tool"]: s for s in r.json()["config"]["steps"]}
    assert d["denoise"]["params"]["strength"] == 0.3
    r2 = env["client"].post("/api/projects/p1/chain/config",
                            json={"overrides": {"transcribe": {"min_note_ms": 80}}})
    assert r2.status_code == 200 and r2.json()["plan"] == {"needs_apply": 2}


# ---------------- REST ----------------


def test_chain_status_static_slots(env):
    _make_project(env)
    body = env["client"].get("/api/projects/p1/chain/status").json()
    assert body["running"] is False and body["saved_only"] is True
    assert [s["tool_id"] for s in body["steps"]] == _HUMMING
    assert all(s["status"] == "pending" for s in body["steps"])
    assert body["progress"] == {"done": 0, "total": 5}


def test_chain_tools_endpoint(env):
    _make_project(env)
    body = env["client"].get("/api/projects/p1/chain/tools").json()
    assert [t["id"] for t in body["tools"]] == _ORDER
    # E1-6：预设清单随 tools 返回
    ids = [p["id"] for p in body["presets"]]
    assert "humming-quicklane" in ids and "song-decompose" in ids


def test_list_presets_scan():
    """E1-6：预设清单全量扫 presets/chains/（shape 与关键项）。"""
    presets = list_presets()
    ids = [p["id"] for p in presets]
    assert "humming-quicklane" in ids and "song-decompose" in ids
    sd = next(p for p in presets if p["id"] == "song-decompose")
    assert sd["name"]["zh"] == "成品歌拆解" and sd["steps_count"] == 13
    assert all({"id", "name", "description", "steps_count"} <= set(p.keys())
               for p in presets)


def test_chain_preset_switch_endpoint(env):
    """E1-6：chain/config 切预设——静态槽位按新预设；非法预设 400（不脏档）。"""
    _make_project(env)
    c = env["client"]
    r = c.post("/api/projects/p1/chain/config", json={"preset": "song-decompose"})
    assert r.status_code == 200 and r.json()["config"]["preset"] == "song-decompose"
    body = c.get("/api/projects/p1/chain/status").json()
    assert body["saved_only"] is True and body["preset"] == "song-decompose"
    assert [s["tool_id"] for s in body["steps"]] == [
        "analyze_grid", "separate",
        "transcribe", "quantize", "snap_scale",
        "transcribe", "quantize",
        "transcribe", "quantize", "snap_scale",
        "transcribe", "quantize", "snap_scale"]
    assert body["progress"] == {"done": 0, "total": 13}
    r2 = c.post("/api/projects/p1/chain/config", json={"preset": "humming-quicklane"})
    assert r2.status_code == 200
    assert [s["tool_id"] for s in
            c.get("/api/projects/p1/chain/status").json()["steps"]] == _HUMMING
    r3 = c.post("/api/projects/p1/chain/config", json={"preset": "nope"})
    assert r3.status_code == 400
    assert load_chain_json(env["dir"] / "p1")["preset"] == "humming-quicklane"


def test_chain_run_uses_saved_preset(env, monkeypatch):
    """E 段1 修复：/chain/run 未带 preset → 回读 chain.json 预设（面板「▶ 运行」路径；曾丢成默认哼唱）。"""
    _project_with_audio(env)
    apply_config(_proj(env), preset_id="song-decompose")
    import tsov.webapp.routes.chain as rc
    captured = {}

    class FakeRunner:
        def __init__(self, proj_, **kw):
            captured.clear()
            captured.update(kw)
            self.running = False

        def start(self, steps=None):
            return {"running": False, "steps": []}

    monkeypatch.setattr(rc, "ChainRunner", FakeRunner)
    c = env["client"]
    r = c.post("/api/projects/p1/chain/run", json={})
    assert r.status_code == 200, r.text
    assert captured["preset_id"] == "song-decompose"
    r2 = c.post("/api/projects/p1/chain/run", json={"preset": "humming-quicklane"})
    assert r2.status_code == 200 and captured["preset_id"] == "humming-quicklane"


def test_chain_tools_reports_saved_preset(env):
    """E 段1 修复：/chain/tools 的 preset 回读 chain.json（面板进入不再闪回默认）。"""
    _make_project(env)
    c = env["client"]
    assert c.get("/api/projects/p1/chain/tools").json()["preset"] == "humming-quicklane"
    c.post("/api/projects/p1/chain/config", json={"preset": "song-decompose"})
    assert c.get("/api/projects/p1/chain/tools").json()["preset"] == "song-decompose"


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_writes_run_meta(env, monkeypatch):
    """E1-7：开跑即写 chain/<run_ts>/run-meta.json（preset/source_track/created_at）。"""
    _project_with_audio(env)
    _patch_tools(monkeypatch)
    r = ChainRunner(_proj(env))
    r.start()
    st = _wait_runner(r)
    meta = json.loads((env["dir"] / "p1" / "chain" / st["run_ts"] / "run-meta.json")
                      .read_text(encoding="utf-8"))
    assert meta["preset"] == "humming-quicklane" and meta["created_at"]
    assert meta["source_track"] == r.source_track


def test_chain_artifact_guard_no_run(env):
    _make_project(env)
    r = env["client"].get("/api/projects/p1/chain/artifact", params={"file": "x.wav"})
    assert r.status_code == 404


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_chain_rest_run_and_artifact(env, monkeypatch):
    _project_with_audio(env)
    _patch_tools(monkeypatch)
    client = env["client"]
    r = client.post("/api/projects/p1/chain/run", json={})
    assert r.status_code == 200 and r.json()["ok"] is True
    st = _wait_http(client, "p1")
    assert [s["status"] for s in st["steps"]] == ["done"] * 5
    assert not st.get("saved_only")
    a = client.get("/api/projects/p1/chain/artifact", params={"file": "denoise.out"})
    assert a.status_code == 200 and a.content == b"x"
    bad = client.get("/api/projects/p1/chain/artifact", params={"file": "../score.json"})
    assert bad.status_code == 400
    miss = client.get("/api/projects/p1/chain/artifact", params={"file": "nope.wav"})
    assert miss.status_code == 404
    c = client.post("/api/projects/p1/chain/cancel")
    assert c.status_code == 409


# ---------------- 段3：apply 装配（进工程·处理版单轨，10-09）----------------


def test_load_run_notes(tmp_path):
    run = tmp_path / "r1"
    run.mkdir()
    (run / "03-voice.json").write_text(
        json.dumps({"notes": [{"start": 0, "end": 1, "pitch_midi": 60}]}), encoding="utf-8")
    (run / "05-notes-snapped.json").write_text(
        json.dumps([{"start": 0.1, "end": 1, "pitch_midi": 61}]), encoding="utf-8")
    got = load_run_notes(run)
    assert [n["pitch_midi"] for n in got["raw"]] == [60]
    assert [n["pitch_midi"] for n in got["processed"]] == [61]
    assert load_run_notes(tmp_path / "none") == {"raw": None, "processed": None}


def test_build_apply_first_run():
    tracks = [{"name": "melody", "kind": "midi"}, {"name": "录音", "kind": "audio"}]
    plan = build_apply_commands(tracks, source_track=1, notes=[3])
    assert [(c["op"], c.get("track")) for c in plan["commands"]] == [
        ("add_track", 0), ("set_notes", 2)]
    assert plan["commands"][0]["value"] == {"name": "录音 · 处理", "at": 2, "unique": False}
    assert plan["meta"]["names"] == ["录音 · 处理"]
    assert plan["meta"]["notes"] == 1


def test_build_apply_mount_source():
    tracks = [{"name": "melody", "kind": "midi"}, {"name": "录音", "kind": "audio"}]
    plan = build_apply_commands(tracks, source_track=1, notes=[3],
                                mount={"source_id": "sf2:0123456789abcdef"})
    assert plan["meta"]["mount_source"] == "sf2:0123456789abcdef"
    assert plan["commands"][-1] == {
        "op": "mount_source", "track": 2,
        "value": {"source_id": "sf2:0123456789abcdef"},
    }


def test_build_apply_source_clip_start():
    tracks = [{"name": "录音", "kind": "audio",
               "audio": {"clips": [{"file": "audio/take.wav", "start": 4.25,
                                     "src_offset": 0.0, "src_len": None}]}}]
    plan = build_apply_commands(tracks, source_track=0, notes=[{"pitch_midi": 60,
                                                               "start": 0.5, "end": 1.0}])
    move = next(c for c in plan["commands"] if c["op"] == "move_part")
    assert move["track"] == 1 and move["value"]["start"] == 4.25


def test_build_apply_rerun_idempotent():
    tracks = [{"name": "melody", "kind": "midi"}, {"name": "录音", "kind": "audio"},
              {"name": "录音", "kind": "midi"}, {"name": "录音 · 处理", "kind": "midi"}]
    plan = build_apply_commands(tracks, source_track=1, notes=[])
    ops = [(c["op"], c.get("track")) for c in plan["commands"]]
    assert ops[:2] == [("remove_track", 3), ("remove_track", 2)]        # 从后往前删（含旧版原始轨遗留）
    assert ops[2:] == [("add_track", 0), ("set_notes", 2)]
    assert plan["meta"]["removed"] == [2, 3]
    assert plan["meta"]["inserted"] == [2]


def test_build_apply_old_before_source():
    tracks = [{"name": "录音", "kind": "midi"}, {"name": "melody", "kind": "midi"},
              {"name": "录音", "kind": "audio"}]
    plan = build_apply_commands(tracks, source_track=2, notes=[])
    assert plan["meta"]["removed"] == [0]
    assert plan["commands"][1]["value"]["at"] == 2                       # 源轨删后前移 → 重算
    assert plan["meta"]["inserted"] == [2]


def test_build_apply_source_guard():
    with pytest.raises(ValueError):
        build_apply_commands([], source_track=0, notes=[])


def test_chain_apply_guard_no_run(env):
    _make_project(env)
    r = env["client"].post("/api/projects/p1/chain/apply", json={})
    assert r.status_code == 409


def test_chain_apply_guard_missing_artifacts(env):
    _make_project(env)
    root = env["dir"] / "p1"
    (root / "chain" / "20990101-000000").mkdir(parents=True)
    (root / "chain" / "20990101-000000" / "03-voice.json").write_text(
        json.dumps({"notes": []}), encoding="utf-8")
    (root / "chain.json").write_text(json.dumps(
        {"source_track": 0, "run_ts": "20990101-000000", "preset": "humming-quicklane",
         "steps": []}), encoding="utf-8")
    r = env["client"].post("/api/projects/p1/chain/apply", json={})
    assert r.status_code == 409 and "缺处理版" in r.json()["error"]


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_chain_apply_rest_single_track(env, monkeypatch):
    """段3：链产物进工程（处理版单轨，10-09）——REST 一条路径：轨数 +1 / 只落最终音符 / 幂等 / undo 可回。"""
    _project_with_audio(env)
    _patch_tools(monkeypatch)
    client = env["client"]
    r = client.post("/api/projects/p1/chain/run", json={})
    assert r.status_code == 200
    st = _wait_http(client, "p1")
    assert [s["status"] for s in st["steps"]] == ["done"] * 5
    saved = load_chain_json(env["dir"] / "p1")
    run_dir = env["dir"] / "p1" / "chain" / saved["run_ts"]
    raw = [{"start": 0.0, "end": 0.4, "pitch_midi": 60},
           {"start": 0.5, "end": 0.9, "pitch_midi": 62}]
    proc = [{"start": 0.0, "end": 0.5, "pitch_midi": 60},
            {"start": 0.5, "end": 1.0, "pitch_midi": 63}]
    (run_dir / "03-voice.json").write_text(json.dumps({"notes": raw}), encoding="utf-8")
    (run_dir / "05-notes-snapped.json").write_text(json.dumps(proc), encoding="utf-8")

    proj = _proj(env)
    src_idx = saved["source_track"]
    src_name = proj.score.tracks[src_idx].name

    a1 = client.post("/api/projects/p1/chain/apply", json={})
    assert a1.status_code == 200, a1.text
    body = a1.json()
    assert body["ok"] is True and body["tracks"] == 3
    assert body["names"] == [src_name + " · 处理"]
    proj = _proj(env)
    assert [t.name for t in proj.score.tracks] == ["melody", src_name,
                                                   src_name + " · 处理"]
    assert [n.pitch_midi for n in proj.score.tracks[2].notes] == [60, 63]   # 只落最终（处理版）

    # 幂等：重跑不增轨（先删旧同名轨）
    a2 = client.post("/api/projects/p1/chain/apply", json={})
    assert a2.status_code == 200 and a2.json()["tracks"] == 3
    proj = _proj(env)
    assert [t.name for t in proj.score.tracks] == ["melody", src_name,
                                                   src_name + " · 处理"]

    # undo 可回（生效即可：journal 线性游标不保证单步——循环撤销直到回退到进工程前）
    for _ in range(6):
        proj = _proj(env)
        if len(proj.score.tracks) == 2:
            break
        u = client.post("/api/projects/p1/undo")
        assert u.status_code == 200
    proj = _proj(env)
    assert [t.name for t in proj.score.tracks] == ["melody", src_name]

def test_quantize_tool_triplet_grid(tmp_path):
    """v0.2 批C 后段（C7）：三连音网格 grid=3/6（cell = 拍/3、拍/6；命令层/工具层原生支持）。"""
    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.flac"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    voice = {"bpm": 120.0, "notes": [
        {"start": 0.17, "end": 0.33, "pitch_midi": 60, "pitch_hz": 261.6, "deviation_cents": 0.0},
        {"start": 0.51, "end": 0.68, "pitch_midi": 62, "pitch_hz": 293.7, "deviation_cents": 0.0},
    ]}
    (run_dir / "03-voice.json").write_text(json.dumps(voice), encoding="utf-8")
    ctx = _ctx(proj_root, run_dir, src, "quantize", {"grid": 3, "strength": 1.0},
               prev=run_dir / "03-voice.json")
    out = get_tool("quantize")["run"](ctx)
    q = json.loads((run_dir / out["artifact"]).read_text(encoding="utf-8"))
    cell = (60.0 / 120.0) / 3.0                      # 1/8 三连 = 1/3 拍 = 0.1666667s
    assert out["grid"] == 3
    assert q[0]["start"] == pytest.approx(cell, abs=1e-6)        # 0.17 → 1/3 拍
    assert q[1]["start"] == pytest.approx(3 * cell, abs=1e-6)    # 0.51 → 1 拍（3 格）
    ctx6 = _ctx(proj_root, run_dir, src, "quantize", {"grid": 6, "strength": 1.0},
                prev=run_dir / "03-voice.json")
    out6 = get_tool("quantize")["run"](ctx6)
    q6 = json.loads((run_dir / out6["artifact"]).read_text(encoding="utf-8"))
    cell6 = (60.0 / 120.0) / 6.0                     # 1/16 三连 = 1/6 拍
    assert q6[0]["start"] == pytest.approx(2 * cell6, abs=1e-6)  # 0.17 → 2/6 拍


# ---------------- 批E 段1：定速与对齐（E1-1）----------------


def _write_click_wav(path, *, bpm=130.0, phase=0.2, seconds=15.0, sr=22050):
    """合成拍点素材：每拍短脉冲（噪声衰减包络），拍点位于 phase + k·周期。"""
    rng = np.random.default_rng(42)
    y = np.zeros(int(seconds * sr), dtype=np.float32)
    period = 60.0 / bpm
    t = phase
    while t < seconds - 0.25:
        i = int(round(t * sr))
        n = min(600, len(y) - i)
        env = np.exp(-np.arange(n) / 80.0).astype(np.float32)
        y[i:i + n] += (0.9 * env * rng.standard_normal(n)).astype(np.float32)
        t += period
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), y, sr, subtype="FLOAT")
    return path


def test_quantize_notes_phase_regression():
    """E1-1：phase_s 缺省 = 旧行为逐字一致；给定相位 → 音符吸到相位网格。"""
    from tsov.core.notes import Note
    from tsov.dsp.quantize import quantize_notes

    def note(s, e):
        return Note(start=s, end=e, pitch_midi=60, pitch_hz=261.6, deviation_cents=0.0)

    notes = [note(0.03, 0.26), note(0.51, 0.74), note(0.98, 1.2)]
    bpm, grid = 120.0, 16
    cell = (60.0 / bpm) / grid
    got = quantize_notes(notes, bpm=bpm, grid=grid)
    exp = [(round(n.start / cell) * cell, round(n.end / cell) * cell) for n in notes]
    assert [(x.start, x.end) for x in got] == [(round(s, 6), round(e, 6)) for s, e in exp]

    phase = 0.013
    pn = [note(phase + 2 * cell + 0.004, phase + 4 * cell - 0.001),
          note(phase + 8 * cell + 0.006, phase + 9 * cell - 0.003)]
    pq = quantize_notes(pn, bpm=bpm, grid=grid, phase_s=phase)
    assert pq[0].start == pytest.approx(phase + 2 * cell, abs=1e-9)
    assert pq[0].end == pytest.approx(phase + 4 * cell, abs=1e-9)
    assert pq[1].start == pytest.approx(phase + 8 * cell, abs=1e-9)


def test_analyze_grid_tool_synthetic(tmp_path):
    """E1-1：合成拍点素材真跑——BPM 130±0.5、相位 ±15ms、grid.json 五字段。"""
    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = _write_click_wav(proj_root / "audio" / "click.wav", bpm=130.0, phase=0.2, seconds=15.0)
    ctx = _ctx(proj_root, run_dir, src, "analyze_grid", {})
    out = get_tool("analyze_grid")["run"](ctx)
    assert abs(out["bpm"] - 130.0) <= 0.5, out
    assert abs(out["beat_offset"] - 0.2) <= 0.015, out
    assert out["beats_count"] >= 20
    assert out["duration_sec"] == pytest.approx(15.0, abs=0.1)
    data = json.loads((run_dir / "grid.json").read_text(encoding="utf-8"))
    assert set(data) == {"bpm", "beat_offset", "beats", "confidence", "duration_sec"}
    assert len(data["beats"]) == out["beats_count"]


def test_analyze_grid_override_offset(tmp_path):
    """E1-1：bpm_override / offset_ms 手调口生效。"""
    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = _write_click_wav(proj_root / "audio" / "click.wav", bpm=130.0, phase=0.2)
    tool_run = get_tool("analyze_grid")["run"]

    ctx = _ctx(proj_root, run_dir, src, "analyze_grid", {"bpm_override": "131"})
    out = tool_run(ctx)
    assert out["bpm"] == 131.0 and out["bpm_detected"] != 131.0

    base = tool_run(_ctx(proj_root, run_dir, src, "analyze_grid", {}))
    ctx2 = _ctx(proj_root, run_dir, src, "analyze_grid", {"offset_ms": 100})
    out2 = tool_run(ctx2)
    period = 60.0 / out2["bpm"]
    expect = (base["beat_offset"] + 0.1) % period
    assert out2["beat_offset"] == pytest.approx(expect, abs=0.02)


def test_quantize_reads_grid_history(tmp_path):
    """E1-1：_run_quantize 读 grid.json——bpm 优先 grid、相位应用；历史 run 回退。"""
    proj_root = tmp_path / "proj"
    hist = proj_root / "chain" / "r1"       # 历史 run（grid.json 在此）
    run_dir = proj_root / "chain" / "r2"    # 本次 run（「只重跑下游步」）
    hist.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.flac"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")

    bpm, phase = 130.0, 0.2
    (hist / "grid.json").write_text(json.dumps(
        {"bpm": bpm, "beat_offset": phase, "beats": [], "confidence": 1.0, "duration_sec": 10.0}),
        encoding="utf-8")
    voice = {"bpm": 90.0, "notes": [
        {"start": phase + 0.01, "end": phase + 0.3, "pitch_midi": 60, "pitch_hz": 261.6,
         "deviation_cents": 0.0},
        {"start": phase + 0.5, "end": phase + 0.9, "pitch_midi": 62, "pitch_hz": 293.7,
         "deviation_cents": 0.0}]}
    (run_dir / "03-voice.json").write_text(json.dumps(voice), encoding="utf-8")
    ctx = _ctx(proj_root, run_dir, src, "quantize", {"grid": 4, "strength": 1.0},
               prev=run_dir / "03-voice.json")
    out = get_tool("quantize")["run"](ctx)
    assert out["bpm"] == bpm and out["bpm_source"] == "grid"
    assert out["phase_s"] == pytest.approx(phase, abs=1e-9)
    q = json.loads((run_dir / out["artifact"]).read_text(encoding="utf-8"))
    cell = (60.0 / bpm) / 4
    for n in q:
        rel = (n["start"] - phase) / cell
        assert abs(rel - round(rel)) < 1e-3, n


def test_quantize_bpm_priority(tmp_path):
    """E1-1：bpm 优先级 步参数 > grid > 音符文件 > 工程 tempo；覆盖时相位按新周期重算。"""
    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.flac"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    beats = [0.2 + k * (60.0 / 130.0) for k in range(64)]
    (run_dir / "grid.json").write_text(json.dumps(
        {"bpm": 130.0, "beat_offset": 0.2, "beats": beats, "confidence": 1.0,
         "duration_sec": 30.0}), encoding="utf-8")
    voice = {"bpm": 90.0, "notes": [
        {"start": 0.21, "end": 0.5, "pitch_midi": 60, "pitch_hz": 261.6, "deviation_cents": 0.0}]}
    (run_dir / "03-voice.json").write_text(json.dumps(voice), encoding="utf-8")

    # 1) 步参数覆盖：140 → 相位按 beats × 新周期重算
    ctx = _ctx(proj_root, run_dir, src, "quantize", {"grid": 4, "strength": 1.0, "bpm": "140"},
               prev=run_dir / "03-voice.json")
    out = get_tool("quantize")["run"](ctx)
    assert out["bpm"] == 140.0 and out["bpm_source"] == "param"
    exp_phase = float(np.median(np.mod(np.asarray(beats, dtype=float), 60.0 / 140.0)))
    assert out["phase_s"] == pytest.approx(exp_phase, abs=1e-6)

    # 2) 无步参数 → grid 优先（bpm=130、相位=beat_offset）
    ctx2 = _ctx(proj_root, run_dir, src, "quantize", {"grid": 4, "strength": 1.0},
                prev=run_dir / "03-voice.json")
    out2 = get_tool("quantize")["run"](ctx2)
    assert out2["bpm"] == 130.0 and out2["bpm_source"] == "grid"
    assert out2["phase_s"] == pytest.approx(0.2, abs=1e-9)

    # 3) 无 grid → 音符文件 bpm；再无 → 工程 tempo
    (run_dir / "grid.json").unlink()          # 清掉历史网格（本情形测「无 grid」路径）
    run2 = proj_root / "chain" / "r2"
    run2.mkdir()
    (run2 / "03-voice.json").write_text(json.dumps(voice), encoding="utf-8")
    ctx3 = _ctx(proj_root, run2, src, "quantize", {"grid": 4, "strength": 1.0},
                prev=run2 / "03-voice.json")
    out3 = get_tool("quantize")["run"](ctx3)
    assert out3["bpm"] == 90.0 and out3["bpm_source"] == "voice" and out3["phase_s"] == 0.0

    voice2 = {"notes": voice["notes"]}
    (run2 / "03-voice.json").write_text(json.dumps(voice2), encoding="utf-8")
    out4 = get_tool("quantize")["run"](_ctx(proj_root, run2, src, "quantize",
                                            {"grid": 4, "strength": 1.0},
                                            prev=run2 / "03-voice.json"))
    assert out4["bpm"] == 120.0 and out4["bpm_source"] == "project"


# ---------------- 批E 段1：分离（E1-2）----------------


def test_separate_tool_mocked(tmp_path, monkeypatch):
    """E1-2：separate 快档——假 demucs 产出 → 整理 / manifest / stats。"""
    import tsov.chain.tools as ct

    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.wav"
    _write_wav(src, seconds=1.0, sr=16000)

    def fake_exec(ctx, cmd, *, label, eta_sec, env_extra=None, on_cancel=None,
                  heartbeat_sec=10.0):
        out_dir = Path(cmd[cmd.index("-o") + 1])
        track = out_dir / "htdemucs" / "in"
        track.mkdir(parents=True)
        for s in ("vocals", "drums", "bass", "other"):
            (track / f"{s}.wav").write_bytes(b"RIFF" + s.encode())
        assert env_extra and env_extra.get("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD") == "1"
        return 0, ["fake demucs ok"], 1.5

    monkeypatch.setattr(ct, "_exec_child", fake_exec)
    ctx = _ctx(proj_root, run_dir, src, "separate", {"quality": "fast"})
    out = get_tool("separate")["run"](ctx)
    assert out["artifact"] == "stems/manifest.json"
    assert set(out["stems"]) == {"vocals", "drums", "bass", "other"}
    assert out["elapsed_sec"] == 1.5
    stems_dir = run_dir / "stems"
    for s in ("vocals", "drums", "bass", "other"):
        assert (stems_dir / f"{s}.wav").is_file()
    mf = json.loads((stems_dir / "manifest.json").read_text(encoding="utf-8"))
    assert mf["quality"] == "fast" and mf["model"] == "htdemucs"
    assert mf["stems"]["vocals"]["bytes"] == len(b"RIFFvocals")
    assert not (stems_dir / "_raw").exists()


def test_separate_failure_paths(tmp_path, monkeypatch):
    """E1-2：失败路径——rc≠0 指路报错 / 无产出报错 / quality 档未接入报错。"""
    import tsov.chain.tools as ct

    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.wav"
    _write_wav(src, seconds=1.0, sr=16000)
    tool_run = get_tool("separate")["run"]

    def fail_exec(ctx, cmd, **kw):
        return 1, ["CUDA out of memory", "boom"], 2.0

    monkeypatch.setattr(ct, "_exec_child", fail_exec)
    with pytest.raises(RuntimeError, match="显存不足"):
        tool_run(_ctx(proj_root, run_dir, src, "separate", {"quality": "fast"}))

    def empty_exec(ctx, cmd, **kw):
        return 0, ["no output"], 2.0

    monkeypatch.setattr(ct, "_exec_child", empty_exec)
    with pytest.raises(RuntimeError, match="未按预期产出"):
        tool_run(_ctx(proj_root, run_dir, src, "separate", {"quality": "fast"}))


def test_separate_quality_orchestration(tmp_path, monkeypatch):
    """E1-3：质量档——ft 四轨 + Roformer 人声合并；vocals 取 Roformer 版。"""
    import tsov.chain.tools as ct

    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.wav"
    _write_wav(src, seconds=1.0, sr=16000)

    def fake_exec(ctx, cmd, *, label, eta_sec, env_extra=None, on_cancel=None,
                  heartbeat_sec=10.0):
        if "demucs" in cmd:  # 路① ft
            out = Path(cmd[cmd.index("-o") + 1])
            track = out / "htdemucs_ft" / "in"
            track.mkdir(parents=True)
            for s in ("vocals", "drums", "bass", "other"):
                (track / f"{s}.wav").write_bytes(b"FT" + s.encode())
        else:  # 路② audio-separator
            out = Path(cmd[cmd.index("--output_dir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "in_(vocals)_vocals_mel_band_roformer.wav").write_bytes(b"ROFORMER-vocals")
            assert env_extra and "AUDIO_SEPARATOR_MODEL_DIR" in env_extra
        return 0, ["fake ok"], 1.0

    monkeypatch.setattr(ct, "_exec_child", fake_exec)
    monkeypatch.setattr(ct, "_sep_assets_check", lambda: (True, ""))
    out = get_tool("separate")["run"](
        _ctx(proj_root, run_dir, src, "separate", {"quality": "quality"}))
    assert out["quality"] == "quality"
    assert out["model"] == "htdemucs_ft+vocals_mel_band_roformer"
    assert out["degraded"] is False and out["elapsed_sec"] == 2.0
    assert (run_dir / "stems" / "vocals.wav").read_bytes() == b"ROFORMER-vocals"  # 人声走 Roformer
    assert (run_dir / "stems" / "drums.wav").read_bytes() == b"FTdrums"          # 其余走 ft
    mf = json.loads((run_dir / "stems" / "manifest.json").read_text(encoding="utf-8"))
    assert mf["degraded"] is False and "roformer" in mf["model"]


def test_separate_quality_degrade(tmp_path, monkeypatch):
    """E1-3：质量档缺件 → 自动降级纯 ft（vocals 也用 ft）；日志/stats 指路。"""
    import tsov.chain.tools as ct

    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.wav"
    _write_wav(src, seconds=1.0, sr=16000)

    def fake_exec(ctx, cmd, *, label, eta_sec, env_extra=None, on_cancel=None,
                  heartbeat_sec=10.0):
        assert "audio-separator" not in " ".join(cmd)  # 降级后不得跑人声路
        out = Path(cmd[cmd.index("-o") + 1])
        track = out / "htdemucs_ft" / "in"
        track.mkdir(parents=True)
        for s in ("vocals", "drums", "bass", "other"):
            (track / f"{s}.wav").write_bytes(b"FT" + s.encode())
        return 0, ["fake ok"], 1.0

    monkeypatch.setattr(ct, "_exec_child", fake_exec)
    monkeypatch.setattr(ct, "_sep_assets_check",
                        lambda: (False, "模型缺失。指路：fetch_engines --only sep-model"))
    logs: list = []
    ctx = _ctx(proj_root, run_dir, src, "separate", {"quality": "quality"})
    ctx.on_log = logs.append
    out = get_tool("separate")["run"](ctx)
    assert out["degraded"] is True and out["model"] == "htdemucs_ft"
    assert any("fetch_engines" in m for m in logs)
    assert (run_dir / "stems" / "vocals.wav").read_bytes() == b"FTvocals"
    mf = json.loads((run_dir / "stems" / "manifest.json").read_text(encoding="utf-8"))
    assert mf["degraded"] is True and mf["degrade_reason"]


# ---------------- 批E 段1：转录 stem 参数 + 链预设（E1-4）----------------


def test_transcribe_stem_input_and_naming(tmp_path, monkeypatch):
    """E1-4：stem=vocals——输入取 stems/vocals.wav（本 run→历史回退）、产物 voice-vocals.json。"""
    import importlib

    class FakeVoice:
        bpm = 130.0
        notes: list = []

        def to_dict(self):
            return {"bpm": self.bpm, "notes": []}

    seen: dict = {}

    def fake_transcribe(path, backend="rmvpe", **kw):
        seen.update({"path": str(path), "backend": backend})
        return FakeVoice()

    mod = importlib.import_module("tsov.dsp.transcribe")
    monkeypatch.setattr(mod, "transcribe", fake_transcribe)

    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r2"      # 本次 run（无 stems）
    hist = proj_root / "chain" / "r1"         # 历史 run（有 stems）
    (hist / "stems").mkdir(parents=True)
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.wav"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    (hist / "stems" / "vocals.wav").write_bytes(b"RIFFx")

    ctx = _ctx(proj_root, run_dir, src, "transcribe", {"stem": "vocals", "backend": "rmvpe"})
    out = get_tool("transcribe")["run"](ctx)
    assert out["stem"] == "vocals" and out["artifact"] == "voice-vocals.json"
    assert "r1" in seen["path"] and seen["path"].endswith("vocals.wav")   # 历史回退命中

    # 本 run 有 stems → 优先本 run
    (run_dir / "stems").mkdir()
    (run_dir / "stems" / "vocals.wav").write_bytes(b"RIFFy")
    seen.clear()
    out2 = get_tool("transcribe")["run"](
        _ctx(proj_root, run_dir, src, "transcribe", {"stem": "vocals", "backend": "rmvpe"}))
    assert out2["artifact"] == "voice-vocals.json"
    assert "r2" in seen["path"]

    # 全缺 → 明确报错
    with pytest.raises(RuntimeError, match="找不到声部轨"):
        get_tool("transcribe")["run"](
            _ctx(proj_root, run_dir, src, "transcribe", {"stem": "drums"}))


def test_quantize_snap_stem_naming(tmp_path):
    """E1-4：量化/吸附产物命名随输入派生（voice-vocals → notes-vocals-quantized → -snapped）。"""
    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.flac"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    (run_dir / "voice-vocals.json").write_text(json.dumps(
        {"bpm": 120.0, "notes": [{"start": 0.03, "end": 0.26, "pitch_midi": 60,
                                  "pitch_hz": 261.6, "deviation_cents": 0.0}]}),
        encoding="utf-8")
    ctx = _ctx(proj_root, run_dir, src, "quantize", {"grid": 16, "strength": 1.0},
               prev=run_dir / "voice-vocals.json")
    out = get_tool("quantize")["run"](ctx)
    assert out["artifact"] == "notes-vocals-quantized.json"
    ctx2 = _ctx(proj_root, run_dir, src, "snap_scale", {"key": "auto"},
                prev=run_dir / out["artifact"])
    out2 = get_tool("snap_scale")["run"](ctx2)
    assert out2["artifact"] == "notes-vocals-snapped.json"


def test_song_decompose_preset_shape():
    """E1-4/E2-4：song-decompose 预设在案、13 步序与关键参数正确（鼓不吸附）。"""
    p = load_preset("song-decompose")
    assert p["id"] == "song-decompose"
    assert [s["tool"] for s in p["steps"]] == [
        "analyze_grid", "separate",
        "transcribe", "quantize", "snap_scale",
        "transcribe", "quantize",
        "transcribe", "quantize", "snap_scale",
        "transcribe", "quantize", "snap_scale"]
    st = p["steps"]
    assert st[1]["params"]["quality"] == "quality"
    assert st[2]["params"] == {"stem": "vocals", "backend": "game"}
    assert st[5]["params"] == {"stem": "drums", "backend": "adtof"}
    assert st[7]["params"] == {"stem": "bass", "backend": "mt3"}
    assert st[10]["params"] == {"stem": "other", "backend": "mt3"}
    # 鼓不吸附：drums 转录后仅接量化（st[6]），无 snap
    assert st[6]["tool"] == "quantize" and st[7]["tool"] == "transcribe"


# ---------------- 批E 段1：多轨落位（E1-5）----------------

_DEC_STEMS = {"vocals": "audio/v-1.flac", "drums": "audio/d-1.flac",
              "bass": "audio/b-1.flac", "other": "audio/o-1.flac"}
_DEC_MIDIS = [{"stem": "vocals", "notes": [{"start": 0.0, "end": 1.0, "pitch_midi": 60}]}]


def test_build_decompose_first_run():
    """E1-5：首次落位——4 音频追加 + 1 MIDI 轨 + 挂文件夹；索引/meta 正确。"""
    tracks = [{"name": "melody", "kind": "midi"}, {"name": "录音", "kind": "audio"}]
    plan = build_decompose_commands(tracks, source_track=1, stems=_DEC_STEMS, midis=_DEC_MIDIS)
    ops = [(c["op"], c.get("track")) for c in plan["commands"]]
    assert ops == [("add_audio_track", None)] * 4 + [("add_track", 0), ("set_notes", 6)] \
        + [("set_track_folder", i) for i in (2, 3, 4, 5, 6)]
    assert plan["commands"][0]["value"] == {"file": "audio/v-1.flac", "name": "人声"}
    assert plan["commands"][4]["value"] == {"name": "人声 MIDI", "at": 6, "unique": False}
    assert plan["commands"][5]["value"]["notes"][0]["pitch_midi"] == 60
    assert plan["meta"]["folder"] == "拆解 · 录音"
    assert plan["meta"]["audio_tracks"] == [2, 3, 4, 5]
    assert plan["meta"]["midi_tracks"] == [6]
    assert plan["meta"]["names"] == ["人声", "鼓", "贝斯", "其他", "人声 MIDI"]
    assert plan["meta"]["removed"] == []


def test_build_decompose_rerun_idempotent():
    """E1-5：重跑——先删同文件夹旧产物（倒序）再重建，索引重算。"""
    tracks = [{"name": "melody", "kind": "midi"},
              {"name": "录音", "kind": "audio"},
              {"name": "人声", "kind": "audio", "folder": "拆解 · 录音"},
              {"name": "鼓", "kind": "audio", "folder": "拆解 · 录音"},
              {"name": "贝斯", "kind": "audio", "folder": "拆解 · 录音"},
              {"name": "其他", "kind": "audio", "folder": "拆解 · 录音"},
              {"name": "人声 MIDI", "kind": "midi", "folder": "拆解 · 录音"}]
    plan = build_decompose_commands(tracks, source_track=1, stems=_DEC_STEMS, midis=_DEC_MIDIS)
    assert plan["meta"]["removed"] == [2, 3, 4, 5, 6]
    ops = [(c["op"], c.get("track")) for c in plan["commands"]]
    assert ops[:5] == [("remove_track", i) for i in (6, 5, 4, 3, 2)]
    assert plan["meta"]["audio_tracks"] == [2, 3, 4, 5]
    assert plan["meta"]["midi_tracks"] == [6]


def test_build_decompose_folder_scope():
    """E1-5：清理范围＝同文件夹（别文件夹同名轨不删；源轨即使在该文件夹也不删）；缺参报错。"""
    tracks = [{"name": "melody", "kind": "midi"},
              {"name": "录音", "kind": "audio", "folder": "拆解 · 录音"},
              {"name": "人声", "kind": "audio", "folder": "别的文件夹"}]
    plan = build_decompose_commands(tracks, source_track=1, stems=_DEC_STEMS, midis=[])
    assert plan["meta"]["removed"] == []
    assert plan["meta"]["midi_tracks"] == []
    with pytest.raises(ValueError):
        build_decompose_commands(tracks, source_track=0, stems={"vocals": "x"}, midis=[])
    with pytest.raises(ValueError):
        build_decompose_commands(tracks, source_track=9, stems=_DEC_STEMS, midis=[])


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_decompose_apply_rest(env):
    """E1-5 双通道·agent 路：假拆解产物 → REST apply → 文件夹/4 音频/1 MIDI/tempo/幂等/undo。"""
    _project_with_audio(env)
    client = env["client"]
    root = env["dir"] / "p1"
    proj = _proj(env)
    src_name = proj.score.tracks[1].name
    folder = f"拆解 · {src_name}"

    run_ts = "20990101-000000"
    run_dir = root / "chain" / run_ts
    (run_dir / "stems").mkdir(parents=True)
    for s in ("vocals", "drums", "bass", "other"):
        _write_wav(run_dir / "stems" / f"{s}.wav", seconds=0.5, sr=16000)
    notes = [{"start": 0.0, "end": 0.4, "pitch_midi": 60},
             {"start": 0.5, "end": 0.9, "pitch_midi": 63}]
    (run_dir / "notes-vocals-snapped.json").write_text(
        json.dumps({"notes": notes}), encoding="utf-8")
    (run_dir / "grid.json").write_text(json.dumps(
        {"bpm": 129.999, "beat_offset": 0.003, "beats": [], "confidence": 1.0,
         "duration_sec": 1.0}), encoding="utf-8")
    (root / "chain.json").write_text(json.dumps(
        {"source_track": 1, "preset": "song-decompose", "run_ts": run_ts, "steps": []}),
        encoding="utf-8")

    r = client.post("/api/projects/p1/chain/apply", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["folder"] == folder
    assert body["audio_tracks"] == [2, 3, 4, 5] and body["midi_tracks"] == [6]
    assert body["tempo"]["to"] == 129.999 and body["tempo"]["applied"] is True
    assert body["notes_source"] == "notes-vocals-snapped.json"

    proj = _proj(env)
    assert [t.name for t in proj.score.tracks] == ["melody", src_name, "人声", "鼓", "贝斯",
                                                   "其他", "人声 MIDI"]
    assert proj.score.tempo == 129.999
    for t in proj.score.tracks[2:6]:
        assert t.kind == "audio" and t.folder == folder
        assert (proj.root / t.audio["file"]).is_file()
    mid = proj.score.tracks[6]
    assert mid.kind == "midi" and mid.folder == folder and len(mid.notes) == 2
    assert [n.pitch_midi for n in mid.notes] == [60, 63]

    # 幂等：重跑不增轨、名字不变
    r2 = client.post("/api/projects/p1/chain/apply", json={})
    assert r2.status_code == 200 and r2.json()["removed"] == [2, 3, 4, 5, 6]
    proj = _proj(env)
    assert [t.name for t in proj.score.tracks] == ["melody", src_name, "人声", "鼓", "贝斯",
                                                   "其他", "人声 MIDI"]

    # undo 可回（循环撤销直到回退到落位前）
    for _ in range(16):
        proj = _proj(env)
        if len(proj.score.tracks) == 2:
            break
        u = client.post("/api/projects/p1/undo")
        assert u.status_code == 200
    proj = _proj(env)
    assert [t.name for t in proj.score.tracks] == ["melody", src_name]


def test_exec_child_normal_and_cancel(tmp_path):
    """E1-2：_exec_child——正常收集输出；取消→杀子进程树（ChainCancelled + pid 检死）。"""
    import subprocess
    import sys

    from tsov.chain.tools import _exec_child

    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.wav"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")

    def ctx_new():
        return _ctx(proj_root, run_dir, src, "separate", {})

    # 1) 正常：输出收集
    ctx = ctx_new()
    rc, tail, _el = _exec_child(ctx, [sys.executable, "-c", "print('hello-child')"],
                                label="测试", eta_sec=1.0, heartbeat_sec=0.1)
    assert rc == 0 and any("hello-child" in ln for ln in tail)

    # 2) 取消：子进程写 pid 后长眠 → cancel → 杀树；pid 随后检死
    pid_file = tmp_path / "child.pid"
    code = ("import os,sys,time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(120)\n")
    ctx2 = ctx_new()
    cleaned: list = []

    def killer():
        time.sleep(2.0)
        ctx2.cancel.set()

    threading.Thread(target=killer, daemon=True).start()
    t0 = time.time()
    with pytest.raises(Exception, match="已取消"):
        _exec_child(ctx2, [sys.executable, "-c", code, str(pid_file)],
                    label="测试", eta_sec=120.0, heartbeat_sec=5.0,
                    on_cancel=lambda: cleaned.append(True))
    assert time.time() - t0 < 40
    assert cleaned == [True]
    assert pid_file.is_file()
    pid = int(pid_file.read_text(encoding="utf-8"))

    def pid_alive() -> bool:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        return str(pid) in (r.stdout or "")

    alive = pid_alive()
    for _ in range(10):
        if not alive:
            break
        time.sleep(0.5)
        alive = pid_alive()
    assert not alive, f"子进程 {pid} 未被杀干净"



# ---------------- 批E 段2：鼓转录（E2-3）----------------


def test_transcribe_adtof_spawn_mocked(tmp_path, monkeypatch):
    """E2-3：transcribe（adtof）走子进程形态——_exec_child 拉起 spawn 模块；产物原子读回。"""
    import tsov.chain.tools as ct

    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.wav"
    _write_wav(src, seconds=1.0, sr=16000)
    stems = run_dir / "stems"
    stems.mkdir(parents=True)
    (stems / "drums.wav").write_bytes(b"RIFF....WAVE")  # _find_stem_wav 命中（内容不解析）

    seen: dict = {}

    def fake_exec(ctx, cmd, *, label, eta_sec, env_extra=None, on_cancel=None,
                  heartbeat_sec=10.0):
        seen["cmd"] = list(cmd)
        seen["env"] = dict(env_extra or {})
        out = Path(cmd[5])
        out.write_text(json.dumps({
            "bpm": 120.0,
            "notes": [{"start": 0.5, "end": 0.6, "pitch_midi": 35, "pitch_hz": 61.7,
                       "velocity": 0.8, "confidence": 1.0, "deviation_cents": 0.0,
                       "is_ornament": False}]}, ensure_ascii=False), encoding="utf-8")
        return 0, ["[spawn] OK"], 0.9

    monkeypatch.setattr(ct, "_exec_child", fake_exec)
    ctx = _ctx(proj_root, run_dir, src, "transcribe", {"backend": "adtof", "stem": "drums"})
    out = get_tool("transcribe")["run"](ctx)

    assert out["artifact"] == "voice-drums.json"
    assert out["backend"] == "adtof" and out["stem"] == "drums"
    assert out["notes"] == 1 and out["bpm"] == 120.0 and out["empty"] is False
    cmd = seen["cmd"]
    assert cmd[1] == "-m" and cmd[2] == "tsov.dsp.backends.spawn"
    assert cmd[3] == "adtof" and cmd[4].endswith("drums.wav")
    assert cmd[5].endswith("voice-drums.json")
    assert json.loads(cmd[6]) == {}  # 无额外参数透传
    assert seen["env"].get("PYTHONUTF8") == "1" and seen["env"].get("PYTHONPATH")


def test_transcribe_adtof_spawn_failure_cleans(tmp_path, monkeypatch):
    """E2-3：子进程失败 → 指路报错 + 半成品零残留。"""
    import tsov.chain.tools as ct

    proj_root = tmp_path / "proj"
    run_dir = proj_root / "chain" / "r1"
    run_dir.mkdir(parents=True)
    src = proj_root / "audio" / "in.wav"
    _write_wav(src, seconds=1.0, sr=16000)
    (run_dir / "stems").mkdir(parents=True)
    (run_dir / "stems" / "drums.wav").write_bytes(b"RIFF")

    def fail_exec(ctx, cmd, **kw):
        (run_dir / "voice-drums.json").write_bytes(b"partial")  # 模拟半成品
        return 1, ["Traceback", "CUDA out of memory"], 0.5

    monkeypatch.setattr(ct, "_exec_child", fail_exec)
    with pytest.raises(RuntimeError, match="转录失败（adtof"):
        get_tool("transcribe")["run"](_ctx(proj_root, run_dir, src, "transcribe",
                                           {"backend": "adtof", "stem": "drums"}))
    assert not (run_dir / "voice-drums.json").exists()


def test_transcribe_backend_options_include_adtof():
    t = get_tool("transcribe")
    opts = t["params"]["backend"]["options"]
    assert "adtof" in opts and "mt3" in opts



# ---------------- 批E 段2：重复工具寻址（E2-4/E2-5）----------------


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_dup_tool_positional_inheritance(env):
    """E2：重复工具（transcribe×4）——同序 chain.json 按位继承（精确到步，不串味）。"""
    _project_with_audio(env)
    proj = _proj(env)
    preset = load_preset("song-decompose")
    steps = [{"tool": e["tool"], "status": "pending"} for e in preset["steps"]]
    steps[2]["status"] = "done"
    steps[7]["status"] = "done"
    steps[7]["params"] = {"min_note_ms": 123.0}
    (env["dir"] / "p1" / "chain.json").write_text(json.dumps(
        {"source_track": 1, "preset": "song-decompose", "run_ts": None, "steps": steps}),
        encoding="utf-8")
    r = ChainRunner(proj, preset_id="song-decompose", source_track=1)
    assert len(r.steps) == 13
    assert r.steps[7].params["stem"] == "bass" and r.steps[7].params["min_note_ms"] == 123.0
    assert r.steps[2].params["stem"] == "vocals" and r.steps[2].params["min_note_ms"] == 60.0
    assert r.steps[5].params["stem"] == "drums" and r.steps[10].params["stem"] == "other"
    assert r.steps[7].status == "done" and r.steps[2].status == "done"
    assert r.steps[6].status == "pending"


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_cross_preset_no_bleed(env):
    """E2：换预设（异序）——状态不回填、参数不继承（防串参）。"""
    _project_with_audio(env)
    proj = _proj(env)
    old = [{"tool": t, "status": "done",
            "params": ({"min_note_ms": 88.0} if t == "transcribe" else {})}
           for t in ("denoise", "loudnorm", "transcribe", "quantize", "snap_scale")]
    (env["dir"] / "p1" / "chain.json").write_text(json.dumps(
        {"source_track": 1, "preset": "humming-quicklane", "run_ts": "20260101-000000",
         "steps": old}), encoding="utf-8")
    r = ChainRunner(proj, preset_id="song-decompose", source_track=1)
    assert all(s.status == "pending" for s in r.steps)
    assert r.steps[2].params["backend"] == "game" and r.steps[2].params["min_note_ms"] == 60.0


def test_apply_config_step_addressing(env):
    """E2：步序寻址——step_mute/step_overrides 精确到步；工具名寻址命中全部出现。"""
    _make_project(env)
    proj = _proj(env)
    data = apply_config(proj, preset_id="song-decompose")
    assert len(data["steps"]) == 13
    data = apply_config(proj, step_mute={"7": True}, step_overrides={"3": {"strength": 0.5}})
    steps = data["steps"]
    assert steps[7]["mute"] is True and steps[2].get("mute") in (None, False)
    assert steps[3]["params"]["strength"] == 0.5
    data2 = apply_config(proj, mute={"quantize": True})
    assert all(s["mute"] for s in data2["steps"] if s["tool"] == "quantize")
    assert not data2["steps"][2].get("mute")
    with pytest.raises(ValueError):
        apply_config(proj, step_mute={99: True})


def test_apply_config_mount_persists(env):
    _make_project(env)
    proj = _proj(env)
    data = apply_config(proj, apply_mount={"source_id": "vst3:abc", "preset_id": "p1"})
    assert data["apply"]["mount"] == {"source_id": "vst3:abc", "preset_id": "p1"}
    assert load_chain_json(proj.root)["apply"]["mount"]["source_id"] == "vst3:abc"
    data2 = apply_config(proj, apply_mount={})
    assert "apply" not in data2


@pytest.mark.skipif(not _HAS_FFMPEG, reason="需要 ffmpeg")
def test_runner_step_index_subset(env, monkeypatch):
    """E2：run 子集按步序定位（重复工具的唯一正解）——[7,9] 只跑第 8/10 步。"""
    _project_with_audio(env)
    calls = _patch_tools(monkeypatch)
    proj = _proj(env)
    r = ChainRunner(proj, preset_id="song-decompose", source_track=1)
    r.start(steps=[7, 9])
    st = _wait_runner(r)
    assert calls == ["transcribe", "snap_scale"]
    assert st["steps"][7]["status"] == "done" and st["steps"][9]["status"] == "done"
    assert st["steps"][6]["status"] == "pending" and st["steps"][10]["status"] == "pending"

