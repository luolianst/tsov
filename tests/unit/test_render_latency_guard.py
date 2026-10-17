"""D2：渲染时延护栏（冷渲 vs 全缓存命中）——即时渲染基础的性能底线。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from tsov.core.score import Score
from tsov.host.cache import StemStore
from tsov.host.engine import HostEngine

from unit._cleanup import rmtree_force

SF2 = Path("vendor/soundfonts/FluidR3_GM.sf2")


def _payload():
    return {
        "title": "lat", "tempo": 120.0, "time_signature": "4/4", "key_candidates": [],
        "tracks": [{
            "name": "m",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 1.0, "effects": []},
            "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 60, "pitch_hz": 261.63,
                       "velocity": 0.8, "confidence": 1.0, "deviation_cents": 0.0, "is_ornament": False},
                      {"start": 0.5, "end": 1.0, "pitch_midi": 64, "pitch_hz": 329.63,
                       "velocity": 0.8, "confidence": 1.0, "deviation_cents": 0.0, "is_ornament": False}],
        }],
        "meta": {},
    }


@pytest.mark.skipif(not SF2.is_file(), reason="缺 SoundFont")
def test_cached_render_much_faster_than_cold():
    root = Path("output") / f"lat-{uuid.uuid4().hex[:10]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        score = Score.from_dict(_payload())
        engine = HostEngine()
        store = StemStore(root)

        s1 = engine.load(score, base_dir=root)
        t0 = time.perf_counter()
        engine.render(s1, cache=store, stereo=True)
        cold = time.perf_counter() - t0
        s1.close()

        s2 = engine.load(score, base_dir=root)
        t0 = time.perf_counter()
        stats: dict = {}
        engine.render(s2, cache=store, stereo=True, stats=stats)
        cached = time.perf_counter() - t0
        s2.close()

        assert stats.get("cached") == ["m"]
        assert cached < max(0.35, cold * 0.8), f"cached={cached:.3f}s cold={cold:.3f}s（缓存路径不应接近冷渲）"
    finally:
        rmtree_force(root)
