"""P0 A1：GET /wav 只读（缺失 404 / 过期 409，绝不在读路径写文件）。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tsov.web import create_app

from unit._cleanup import rmtree_force


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"wavro-{uuid.uuid4().hex[:10]}"
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
        "title": "wtest", "tempo": 120.0, "key_candidates": [],
        "tracks": [{
            "name": "melody",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
            "notes": [{"start": 0.0, "end": 0.25, "pitch_midi": 60, "pitch_hz": 261.63,
                       "velocity": 0.8, "confidence": 0.9, "deviation_cents": 0.0, "is_ornament": False}],
        }],
        "meta": {},
    }


class _Sess:
    def close(self):
        pass


class _Engine:
    samplerate = 44100

    def load(self, score, base_dir=None):
        return _Sess()

    def render(self, session, out_wav=None, stereo=False, cache=None, stats=None, on_progress=None):
        import numpy as np
        import soundfile as sf
        audio = np.zeros((2205, 2), dtype=np.float32)
        if out_wav:
            sf.write(str(out_wav), audio, self.samplerate)
        return audio


def test_wav_missing_is_404_and_does_not_render(env):
    r = env["client"].post("/api/projects", json={"name": "p1", "score": _score_payload()})
    assert r.status_code == 200
    env["app"].state.tsov._engine = _Engine()

    r = env["client"].get("/api/projects/p1/wav")
    assert r.status_code == 404
    body = r.json()
    assert body["stale"] is True and "error" in body
    assert not (env["dir"] / "p1" / "render.wav").exists()   # 读路径没写文件


def test_wav_stale_is_409_without_rewrite(env):
    assert env["client"].post("/api/projects", json={"name": "p1", "score": _score_payload()}).status_code == 200
    env["app"].state.tsov._engine = _Engine()
    assert env["client"].post("/api/projects/p1/render", json={}).status_code == 200

    wav = env["dir"] / "p1" / "render.wav"
    before = wav.stat().st_mtime_ns
    proj = env["app"].state.tsov.get_project("p1")
    time.sleep(0.01)
    proj.score.title = "改过了"          # 谱面变化（save 更新 mtime）
    proj.save()

    r = env["client"].get("/api/projects/p1/wav")
    assert r.status_code == 409
    assert r.json()["stale"] is True
    assert wav.stat().st_mtime_ns == before   # GET 没有重写 wav

    # 前端路径：先 POST 重渲 → GET 恢复 200
    assert env["client"].post("/api/projects/p1/render", json={}).status_code == 200
    r = env["client"].get("/api/projects/p1/wav")
    assert r.status_code == 200 and r.content[:4] == b"RIFF"


def test_wav_rev_missing_is_404(env):
    """rev 未渲染 → 404（不再在 GET 里按版本拼装）。"""
    assert env["client"].post("/api/projects", json={"name": "p1", "score": _score_payload()}).status_code == 200
    env["app"].state.tsov._engine = _Engine()
    r = env["client"].get("/api/projects/p1/wav", params={"rev": "HEAD"})
    assert r.status_code == 404
    assert not (env["dir"] / "p1" / ".mix-cache").exists()
