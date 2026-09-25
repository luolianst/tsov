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
from tsov.chain import (ChainRunner, apply_config, auto_run_plan,
                        list_tools, load_chain_json, load_preset)
from tsov.chain.tools import TOOLS, ChainContext, get_tool
from tsov.web import create_app

from unit._cleanup import rmtree_force

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
_ORDER = ["denoise", "loudnorm", "transcribe", "quantize", "snap_scale"]


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
        "denoise": "fast", "loudnorm": "fast", "transcribe": "slow",
        "quantize": "fast", "snap_scale": "fast"}
    for t in tools:
        assert callable(TOOLS[t["id"]]["run"]) and t["name"]["zh"] and t["params"]
        assert "run" not in t
    assert [s["tool"] for s in load_preset("humming-quicklane")["steps"]] == _ORDER
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
    assert calls == _ORDER
    assert [s["status"] for s in st["steps"]] == ["done"] * 5
    saved = load_chain_json(env["dir"] / "p1")
    assert saved["run_ts"] and [s["tool"] for s in saved["steps"]] == _ORDER
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
    assert auto_run_plan(proj, "denoise") == {"auto": ["denoise", "loudnorm"], "stops_at": "transcribe"}
    assert auto_run_plan(proj, "transcribe") == {"needs_apply": "transcribe"}
    assert auto_run_plan(proj, "quantize") == {"auto": ["quantize", "snap_scale"]}
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
    assert r.json()["plan"] == {"auto": ["denoise", "loudnorm"], "stops_at": "transcribe"}
    d = {s["tool"]: s for s in r.json()["config"]["steps"]}
    assert d["denoise"]["params"]["strength"] == 0.3
    r2 = env["client"].post("/api/projects/p1/chain/config",
                            json={"overrides": {"transcribe": {"min_note_ms": 80}}})
    assert r2.status_code == 200 and r2.json()["plan"] == {"needs_apply": "transcribe"}


# ---------------- REST ----------------


def test_chain_status_static_slots(env):
    _make_project(env)
    body = env["client"].get("/api/projects/p1/chain/status").json()
    assert body["running"] is False and body["saved_only"] is True
    assert [s["tool_id"] for s in body["steps"]] == _ORDER
    assert all(s["status"] == "pending" for s in body["steps"])
    assert body["progress"] == {"done": 0, "total": 5}


def test_chain_tools_endpoint(env):
    _make_project(env)
    body = env["client"].get("/api/projects/p1/chain/tools").json()
    assert [t["id"] for t in body["tools"]] == _ORDER


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
