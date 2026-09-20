"""M-V7 D1（ADR-0018）：内容指纹 / stem 库 / 缓存拼装 单测。

约定：不用 pytest tmp_path（handoff 坑 81）——手动 output/<uuid> 目录 + rmtree_force 清理；
假音源（确定性常数信号）保证数值可手算与逐位比对。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from tsov.core.notes import Note
from tsov.core.score import Effect, Instrument, Score, Track
from tsov.host import HostSession, HostTrack, export_matrix
from tsov.host.cache import StemStore, track_key
from tsov.host.instrument import SoundSource
from tsov.host.mix import render_buses

from unit._cleanup import rmtree_force

SR = 8000


class _SigSource(SoundSource):
    """确定性信号源（值由音符音高决定，便于失效测试）。"""

    def render(self, notes, samplerate, n_frames):
        base = float(sum(int(n.pitch_midi) for n in notes)) / 1000.0
        return np.full((n_frames,), base, dtype=np.float32)


def _track(name="t", pitches=(60,), volume=1.0, program="piano", effects=None):
    return Track(
        name=name,
        instrument=Instrument(program=program, volume=volume, effects=list(effects or [])),
        notes=[Note(start=0.0, end=0.5, pitch_midi=p, pitch_hz=440.0, velocity=0.8) for p in pitches],
    )


def _session(tracks):
    host = [HostTrack(track=t, source=_SigSource()) for t in tracks]
    score = Score(title="t", tempo=100.0, tracks=tracks)
    return HostSession(score, host, samplerate=SR)


def _ws():
    d = Path("output") / f"cctest-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_track_key_stability_and_invalidation():
    """指纹口径（ADR-0018）：推子不入键；音符/音源/效果入键。"""
    t = _track(pitches=(60, 62))
    k0 = track_key(t, SR)
    assert track_key(_track(pitches=(60, 62)), SR) == k0                 # 同内容 → 同键
    assert track_key(_track(pitches=(60, 62), volume=0.3), SR) == k0     # 推子 → 不入键（刀口）
    assert track_key(_track(pitches=(60, 64)), SR) != k0                 # 音符变 → 键变
    assert track_key(_track(pitches=(60, 62), program="strings"), SR) != k0   # 音源变 → 键变
    assert track_key(_track(pitches=(60, 62), effects=[Effect(type="reverb", params={"room_size": 0.5})]), SR) != k0  # 效果变 → 键变
    assert track_key(t, 22050) != k0                                     # 采样率入键


def test_stem_store_roundtrip_and_gc():
    """stem 库：float32 无声损往返；GC 保留引用、清理孤儿与退役目录。"""
    ws = _ws()
    try:
        store = StemStore(ws)
        audio = (np.random.RandomState(0).randn(1000, 2).astype(np.float32) * 0.1)
        key, other = "a" * 24, "b" * 24
        assert not store.has(key)
        store.save(key, audio, SR)
        assert store.has(key) and store.load(key) is not None
        assert np.array_equal(store.load(key), audio)                    # 往返逐位一致
        store.save(other, audio, SR)
        rep = store.gc({key})
        assert rep["removed"] == [other] and rep["kept"] == 1
        assert store.has(key) and not store.has(other)
        legacy = ws / ".render-cache"
        legacy.mkdir()
        rep2 = store.gc({key})
        assert ".render-cache" in rep2["legacy_removed"] and not legacy.exists()
    finally:
        rmtree_force(ws)


def test_render_buses_cache_hit_and_dirty_track():
    """缓存拼装：首渲全量 → 全命中逐位一致；推子不失效；改音只重渲该轨。"""
    ws = _ws()
    try:
        store = StemStore(ws)
        s = _session([_track(name="a", pitches=(60,)), _track(name="b", pitches=(62,))])
        st1 = {}
        a1 = render_buses(s, cache=store, stats=st1)
        assert set(st1.get("rendered", [])) == {"a", "b"} and not st1.get("cached")

        st2 = {}
        a2 = render_buses(s, cache=store, stats=st2)
        assert st2.get("rendered", []) == [] and set(st2.get("cached", [])) == {"a", "b"}
        assert np.array_equal(a1, a2)                                    # 缓存拼装 == 直渲（逐位）

        s.tracks[0].track.instrument.volume = 0.3                        # 推子改动 → 不失效
        st3 = {}
        a3 = render_buses(s, cache=store, stats=st3)
        assert not st3.get("rendered") and len(st3.get("cached", [])) == 2
        assert not np.array_equal(a1, a3)                                # 但混音结果确实变了

        s.tracks[0].track.notes[0].pitch_midi = 61                       # 音符改动 → 只重渲 a
        st4 = {}
        render_buses(s, cache=store, stats=st4)
        assert st4.get("rendered") == ["a"] and st4.get("cached") == ["b"]
    finally:
        rmtree_force(ws)


def test_cache_progress_callback():
    """进度回调：首渲报 render、命中报 cached（web 层 SSE render_progress 的来源）。"""
    ws = _ws()
    try:
        store = StemStore(ws)
        s = _session([_track(name="a", pitches=(60,))])
        ev1 = []
        render_buses(s, cache=store, on_progress=ev1.append)
        assert ev1 and ev1[0]["state"] == "render" and ev1[0]["name"] == "a"
        ev2 = []
        render_buses(s, cache=store, on_progress=ev2.append)
        assert ev2 and ev2[0]["state"] == "cached"
    finally:
        rmtree_force(ws)


def test_export_matrix_with_cache_identical():
    """导出矩阵走 stem 库：mix / stems 产物与不走缓存逐位一致（ADR-0018：导出=拼装+实时层）。"""
    ws = _ws()
    try:
        store = StemStore(ws / "proj")
        s = _session([_track(name="a", pitches=(60,)), _track(name="b", pitches=(62,))])
        r1 = export_matrix(s, ws / "out1", samplerate=SR, midi=False)
        r2 = export_matrix(s, ws / "out2", samplerate=SR, midi=False, cache=store)
        m1, _ = sf.read(r1["mix"], dtype="float32")
        m2, _ = sf.read(r2["mix"], dtype="float32")
        assert np.array_equal(m1, m2)
        s1, _ = sf.read(r1["stems"]["a"], dtype="float32")
        s2, _ = sf.read(r2["stems"]["a"], dtype="float32")
        assert np.array_equal(s1, s2)
    finally:
        rmtree_force(ws)
