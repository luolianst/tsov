"""P0 B1/B2/B3：懒音源（缓存命中零装载）+ 播放走新鲜 wav。"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tsov.core.score import Score
from tsov.host.cache import StemStore
from tsov.host.mix import render_buses
from tsov.host.session import HostSession, _LazySource
from tsov.web import create_app
from tsov.webapp.live_transport import LiveUnsupported

from unit._cleanup import rmtree_force


def _payload():
    return {
        "title": "b", "tempo": 120.0, "time_signature": "4/4", "key_candidates": [],
        "tracks": [{
            "name": "m",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 1.0, "effects": []},
            "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 60, "pitch_hz": 261.63,
                       "velocity": 0.8, "confidence": 0.9, "deviation_cents": 0.0, "is_ornament": False}],
        }],
        "meta": {},
    }


def test_lazy_source_not_created_when_stem_cached():
    root = Path("output") / f"lazy-{uuid.uuid4().hex[:10]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        score = Score.from_dict(_payload())
        session = HostSession.from_score(score, soundfont="dummy.sf2", samplerate=44100, lazy=True)
        assert isinstance(session.tracks[0].source, _LazySource)
        assert session.tracks[0].source._src is None

        store = StemStore(root)
        n_frames = int(round((session.duration + 1.0) * 44100))
        key = store.key_for(score.tracks[0], 44100)
        store.save(key, np.zeros((n_frames, 2), dtype=np.float32), 44100)

        stats: dict = {}
        render_buses(session, samplerate=44100, cache=store, stats=stats)
        assert session.tracks[0].source._src is None        # 缓存命中 → 从未建 synth
        assert stats.get("cached") == ["m"] and not stats.get("rendered")
        session.close()
    finally:
        rmtree_force(root)


class _Sess:
    def close(self):
        pass


class _Engine:
    samplerate = 44100

    def __init__(self):
        self.wav_calls = []
        self.play_calls = []

    def load(self, score, base_dir=None, **kw):
        return _Sess()

    def play(self, session, **kw):
        self.play_calls.append(kw)

    def play_wav(self, path, **kw):
        self.wav_calls.append({"path": str(path), **kw})


class _NoLive:
    """Force these fastpath tests through the legacy engine path."""

    def play(self, *args, **kwargs):
        raise LiveUnsupported("test stub")

    def stop(self, *args, **kwargs):
        return {"ok": True}

    def seek(self, *args, **kwargs):
        return {"ok": True}

    def close(self):
        pass


@pytest.fixture()
def env(monkeypatch):
    d = Path("output") / f"playfast-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tsov.webapp.config.AGENT_SESSION_DIR", str(d / "agent-sessions"))
    app = create_app(output_dir=d)
    app.state.tsov.live = _NoLive()
    client = TestClient(app)
    try:
        yield {"dir": d, "app": app, "client": client}
    finally:
        rmtree_force(d)


def test_play_uses_fresh_wav(env):
    env["client"].post("/api/projects", json={"name": "p", "score": _payload()})
    root = env["dir"] / "p"
    wav = root / "render.wav"
    wav.write_bytes(b"RIFF-dummy")
    # 确保 wav 比 score.json 新
    future = time.time() + 5
    os.utime(wav, (future, future))

    eng = _Engine()
    env["app"].state.tsov._engine = eng
    r = env["client"].post("/api/projects/p/play", json={})
    assert r.status_code == 200
    assert eng.wav_calls and not eng.play_calls
    assert eng.wav_calls[0]["path"].endswith("render.wav")


def test_play_falls_back_to_session_when_stale(env):
    env["client"].post("/api/projects", json={"name": "p", "score": _payload()})
    root = env["dir"] / "p"
    wav = root / "render.wav"
    wav.write_bytes(b"RIFF-dummy")
    old = time.time() - 100
    os.utime(wav, (old, old))          # wav 比 score.json 旧 → 走宿主混音

    eng = _Engine()
    env["app"].state.tsov._engine = eng
    r = env["client"].post("/api/projects/p/play", json={})
    assert r.status_code == 200
    assert eng.play_calls and not eng.wav_calls
