"""P0 A1：渲染并发单飞 + 同谱合并 + 原子写（判别性回归）。"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tsov.web import create_app

from unit._cleanup import rmtree_force


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"renderjob-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        rmtree_force(d)


def _score_payload():
    return {
        "title": "rtest", "tempo": 120.0, "key_candidates": [],
        "tracks": [{
            "name": "melody",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
            "notes": [{"start": 0.0, "end": 0.25, "pitch_midi": 60, "pitch_hz": 261.63,
                       "velocity": 0.8, "confidence": 0.9, "deviation_cents": 0.0, "is_ornament": False}],
        }],
        "meta": {},
    }


def _make_project(env, name="p1"):
    r = env["client"].post("/api/projects", json={"name": name, "score": _score_payload()})
    assert r.status_code == 200 and r.json()["ok"] is True
    return name


class _FakeSession:
    def close(self) -> None:
        pass


class FakeEngine:
    """可控假引擎：记渲染次数、可延时、可失败（故障注入用）。"""

    samplerate = 44100

    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.renders = 0
        self._lock = threading.Lock()

    def load(self, score, base_dir=None):
        return _FakeSession()

    def render(self, session, out_wav=None, stereo=False, cache=None, stats=None, on_progress=None):
        with self._lock:
            self.renders += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            if out_wav:
                Path(out_wav).write_bytes(b"PARTIAL")   # 模拟半截文件
            raise RuntimeError("boom")
        audio = np.zeros((4410, 2), dtype=np.float32)
        if out_wav:
            import soundfile as sf
            sf.write(str(out_wav), audio, self.samplerate)
        return audio


def test_identical_render_merges(env):
    """同谱连续两次 POST：第二次命中完成缓存，只真渲一次。"""
    name = _make_project(env)
    fake = FakeEngine()
    env["app"].state.tsov._engine = fake
    c = env["client"]

    r1 = c.post(f"/api/projects/{name}/render", json={})
    r2 = c.post(f"/api/projects/{name}/render", json={})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["wav"] == r2.json()["wav"]
    assert fake.renders == 1
    assert (env["dir"] / name / "render.wav").is_file()


def test_concurrent_renders_single_flight(env):
    """两线程同时 POST：锁串行 + 同谱合并 → 只真渲一次，两请求都 200。"""
    name = _make_project(env)
    fake = FakeEngine(delay=0.2)
    env["app"].state.tsov._engine = fake
    codes: list[int] = []

    def call():
        c = TestClient(env["app"])
        codes.append(c.post(f"/api/projects/{name}/render", json={}).status_code)

    ts = [threading.Thread(target=call) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert sorted(codes) == [200, 200]
    assert fake.renders == 1


def test_failed_render_keeps_old_wav(env):
    """渲染失败：旧 wav 原样保留、临时件清理、不留半截。"""
    name = _make_project(env)
    wav = env["dir"] / name / "render.wav"
    wav.write_bytes(b"OLDWAV-CTENT")
    before = wav.read_bytes()
    env["app"].state.tsov._engine = FakeEngine(fail=True)

    r = TestClient(env["app"], raise_server_exceptions=False).post(f"/api/projects/{name}/render", json={})
    assert r.status_code == 500
    assert wav.read_bytes() == before
    assert not list(wav.parent.glob("*.tmp-*"))


def test_save_skips_when_unchanged(env):
    """save() 内容未变不写：score.json mtime 稳定（防 wav 无端 stale）。"""
    name = _make_project(env)
    proj = env["app"].state.tsov.get_project(name)
    path = env["dir"] / name / "score.json"
    m1 = path.stat().st_mtime_ns
    time.sleep(0.01)
    proj.save()
    assert path.stat().st_mtime_ns == m1
    proj.score.title = "变化"
    proj.save()
    assert path.stat().st_mtime_ns != m1
