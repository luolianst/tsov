"""P0 B1/B2：懒音源（缓存命中零装载）；S2 收口：旧 wav 播放不再回退。"""

from __future__ import annotations

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


class _NoLive:
    """S2：实时引擎不可用 → /play 必须拒绝，不再回退旧 wav。"""

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


def test_play_rejects_when_live_unsupported(env):
    """S2 收口：sidecar 不支持时直接 409；不回退 sounddevice/wav。"""
    env["client"].post("/api/projects", json={"name": "p", "score": _payload()})
    root = env["dir"] / "p"
    (root / "render.wav").write_bytes(b"RIFF-dummy")
    r = env["client"].post("/api/projects/p/play", json={})
    assert r.status_code == 409
    assert "旧 wav 播放已停用" in r.json()["error"]
