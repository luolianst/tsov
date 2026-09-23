"""M-V8 E2 段 3 录音端点单测：devices / start-status-stop / 409 / 入库 + 命令层入轨。

约定同 test_web：output/<uuid> 隔离 + rmtree_force；不依赖真实音频设备
（record_with_monitor 以假实现替换；devices 端点走真枚举）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

import tsov.host.record as record_mod
import tsov.web
from tsov.web import create_app

from unit._cleanup import rmtree_force


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"webrectest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client, "monkeypatch": monkeypatch}
    finally:
        # 防御：任何用例残留的录音线程先停掉再清目录
        with tsov.web._recorder.lock:
            if tsov.web._recorder.stop_event is not None:
                tsov.web._recorder.stop_event.set()
            th = tsov.web._recorder.thread
        if th is not None:
            th.join(timeout=5)
        with tsov.web._recorder.lock:
            tsov.web._recorder.thread = None
        rmtree_force(d)


def _score_payload():
    return {
        "title": "t",
        "tempo": 120.0,
        "key_candidates": [{"key": "C major", "confidence": 0.7}],
        "tracks": [
            {
                "name": "melody",
                "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
                "notes": [],
            }
        ],
        "meta": {},
    }


def _make_project(env, name="p1"):
    r = env["client"].post("/api/projects", json={"name": name, "score": _score_payload()})
    assert r.status_code == 200 and r.json()["ok"] is True
    return name


def _fake_record(monkeypatch, frames=57600, sr=48000, seen=None):
    """record_with_monitor 假实现：立即写一段可听 wav；支持 stop_event 短等待。"""

    def fake(path, *, seconds=None, device=None, out_device=None, monitor=False,
             samplerate=sr, channels=1, stop_event=None, stream_factory=None,
             block_frames=1024, max_seconds=600.0):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        n = int(frames)
        t = np.arange(n) / samplerate
        sig = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32) if n else np.zeros(1, dtype=np.float32)
        sf.write(str(path), sig, samplerate)
        if seen is not None:
            seen.update({"monitor": bool(monitor), "device": device, "out_device": out_device})
        if stop_event is not None:
            stop_event.wait(timeout=0.6)  # 模拟「录制中」，stop 被 set 即返回
        return {"path": str(path), "frames": n, "seconds": round(n / samplerate, 4),
                "samplerate": samplerate, "channels": channels, "device": device,
                "out_device": out_device, "monitor": bool(monitor), "overflowed": False}

    monkeypatch.setattr(record_mod, "record_with_monitor", fake)
    return fake


def test_record_devices_endpoint(env):
    r = env["client"].get("/api/record/devices")
    assert r.status_code == 200, r.text
    d = r.json()
    assert isinstance(d["inputs"], list) and d["inputs"]
    assert isinstance(d["outputs"], list) and d["outputs"]
    assert "index" in d["inputs"][0] and "name" in d["inputs"][0]


def test_record_start_status_stop_flow(env):
    """双通道链路：start（monitor 透传）→ status → stop → 入库 flac + 命令层入轨。"""
    name = _make_project(env)
    seen = {}
    _fake_record(env["monkeypatch"], seen=seen)

    r = env["client"].post(f"/api/projects/{name}/record/start",
                           json={"device": 1, "out_device": 2, "monitor": True})
    assert r.status_code == 200, r.text
    assert r.json()["recording"] is True and r.json()["monitor"] is True

    s = env["client"].get(f"/api/projects/{name}/record/status").json()
    assert s["recording"] is True and s["elapsed"] is not None and s["error"] is None

    r2 = env["client"].post(f"/api/projects/{name}/record/stop", json={"name": "录的一轨"})
    assert r2.status_code == 200, r2.text
    j = r2.json()
    assert j["seconds"] > 1.0 and j["added"] is True
    assert j["file"].startswith("audio/") and j["file"].endswith(".flac")
    assert (env["dir"] / name / j["file"]).is_file()
    assert seen == {"monitor": True, "device": 1, "out_device": 2}

    # 命令层结果：audio 轨在册（kind/audio + 自定义名）
    st = env["client"].get(f"/api/projects/{name}/state").json()
    tr = st["score"]["tracks"][-1]
    assert tr["kind"] == "audio" and tr["name"] == "录的一轨"
    assert tr["audio"]["file"] == j["file"]
    # 临时 wav 已清（入库转 flac 后不留）
    incoming = env["dir"] / name / "audio" / ".incoming"
    assert not incoming.exists() or not list(incoming.glob("rec-*.wav"))


def test_record_stop_without_start_409(env):
    name = _make_project(env)
    r = env["client"].post(f"/api/projects/{name}/record/stop", json={})
    assert r.status_code == 409


def test_record_double_start_409(env):
    name = _make_project(env)
    _fake_record(env["monkeypatch"])
    r1 = env["client"].post(f"/api/projects/{name}/record/start", json={})
    assert r1.status_code == 200
    r2 = env["client"].post(f"/api/projects/{name}/record/start", json={})
    assert r2.status_code == 409
    r3 = env["client"].post(f"/api/projects/{name}/record/stop", json={})
    assert r3.status_code == 200


def test_record_no_signal_400(env):
    name = _make_project(env)
    _fake_record(env["monkeypatch"], frames=0)
    env["client"].post(f"/api/projects/{name}/record/start", json={})
    r = env["client"].post(f"/api/projects/{name}/record/stop", json={})
    assert r.status_code == 400


def test_record_stop_add_track_false(env):
    """add_track=false：只入库不加轨（参考轨场景可自选）。"""
    name = _make_project(env)
    _fake_record(env["monkeypatch"])
    env["client"].post(f"/api/projects/{name}/record/start", json={})
    r = env["client"].post(f"/api/projects/{name}/record/stop",
                           json={"add_track": False, "name": "仅入库"})
    assert r.status_code == 200
    j = r.json()
    assert j["added"] is False and (env["dir"] / name / j["file"]).is_file()
    st = env["client"].get(f"/api/projects/{name}/state").json()
    kinds = [t.get("kind") for t in st["score"]["tracks"]]
    assert "audio" not in kinds  # 没加轨
