"""M-V8 E4 段2 测试共用：合成音频 / 假渲染器 / facts 骨架 / 两轨 score。

约定：手搓 facts 骨架（facts_stub）用于建议/对拍精确断言；
build_facts 集成测试用 FakeRenderer 注入（不触 fluidsynth）。
"""

from __future__ import annotations

import numpy as np

from tsov.core.notes import Note
from tsov.core.score import Bookmark, Instrument, Score, Track

SR = 44100


def tone(freq=440.0, secs=2.0, amp=0.4) -> np.ndarray:
    t = np.linspace(0.0, secs, int(secs * SR), endpoint=False)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float64)


def notes(n=12, pitch=60, step=0.25, dur=0.2):
    out = []
    for i in range(n):
        out.append(Note(start=i * step, end=i * step + dur, pitch_midi=pitch + (i % 5),
                        pitch_hz=261.63, velocity=0.8, confidence=0.8))
    return out


def score_two(*, bpm=110.0, prog0="0", prog1="", name0="piano", name1="drums",
              vol0=1.0, vol1=1.0, effects0=None, sections=None):
    """最简两轨：track0=钢琴（有音）/ track1=鼓（有音）；可挂 section 书签。"""
    s = Score(title="tt", tempo=bpm, time_signature="4/4", key_candidates=[],
              tracks=[
                  Track(name=name0, instrument=Instrument(program=prog0, volume=vol0,
                                                          effects=list(effects0 or [])),
                        pan=0.0, notes=notes()),
                  Track(name=name1, instrument=Instrument(program=prog1, volume=vol1, effects=[]),
                        pan=0.0, notes=notes(pitch=48)),
              ])
    for i, (lab, a, b) in enumerate(sections or []):
        s.bookmarks.append(Bookmark(scope="project", kind="section", start=a, end=b, label=lab))
    return s


class FakeRenderer:
    """假渲染器（build_facts 注入）：track(i)/mix()/samplerate/close。"""

    def __init__(self, buffers, *, sr=SR, mix=None):
        self.buffers = {int(k): v for k, v in buffers.items()}
        self.samplerate = sr
        self._mix = mix
        self.closed = False
        self.calls: list[int] = []

    def track(self, i):
        self.calls.append(int(i))
        return self.buffers.get(int(i), np.zeros(0))

    def mix(self):
        if self._mix is not None:
            return self._mix
        tot = None
        for b in self.buffers.values():
            if tot is None:
                tot = b.copy()
            elif len(b) == len(tot):
                tot = tot + b
        return tot if tot is not None else np.zeros(0)

    def close(self):
        self.closed = True


def facts_stub(rows, *, targets=None, spectrum_bands=None, mix_peak_dbfs=-6.0, clipping=False,
               sections=None, tempo=110.0):
    """标准 facts 骨架。

    rows: [{index, name, rel_db, rms_dbfs?, volume?, pan?, notes?, families?, effects?, silent?}]
    targets: {track: level_hint_db}（建成 per_track 形状）或 None
    spectrum_bands: {track: [6 floats]}（缺省按 index 造平铺）
    """
    tracks_st, tracks_lv, tracks_sp = [], [], []
    for r in rows:
        i = int(r["index"])
        fams = list(r.get("families", []))
        tracks_st.append({"index": i, "name": r["name"], "kind": r.get("kind", "midi"),
                          "notes": int(r.get("notes", 12)), "register": [60, 72],
                          "program": str(r.get("program", "")), "volume": float(r.get("volume", 1.0)),
                          "pan": float(r.get("pan", 0.0)), "mute": False, "solo": False,
                          "effects": list(r.get("effects", [])), "families": fams})
        silent = bool(r.get("silent", False))
        tracks_lv.append({"index": i, "name": r["name"], "silent": silent, "duration_sec": 30.0,
                          "rms_dbfs": r.get("rms_dbfs", -20.0 if not silent else None),
                          "loudest_win_dbfs": -18.0 if not silent else None,
                          "loudest_win_at": 0.0, "peak_dbfs": -12.0 if not silent else None,
                          "rel_db": r.get("rel_db")})
        bands = (spectrum_bands or {}).get(i, [0.17] * 6)
        tracks_sp.append({"index": i, "name": r["name"], "bands": list(bands), "centroid_hz": 800.0})
    per_track = {}
    if targets is not None:
        for k, hint in targets.items():
            per_track[str(int(k))] = {"role": f"r{int(k)}", "title": "", "level_hint_db": float(hint),
                                      "pan": 0.0, "via": "test"}
    return {
        "project": "stub", "built_at": 0.0, "pack": None,
        "structure": {"tempo": tempo, "time_signature": "4/4", "key": "C major",
                      "duration_sec": 30.0, "notes_total": 24, "samplerate": SR,
                      "tracks": tracks_st, "sections": list(sections or [])},
        "levels": {"window_sec": 4.0, "mix_peak_dbfs": mix_peak_dbfs, "clipping": clipping,
                   "anchor": rows[0]["index"] if rows else None, "tracks": tracks_lv},
        "spectrum": {"bands": [{"lo": lo, "hi": hi, "label": ""}
                               for lo, hi in [(20, 120), (120, 350), (350, 1200),
                                              (1200, 4000), (4000, 10000), (10000, 20000)]],
                     "tracks": tracks_sp},
        "sections_levels": [],
        "targets": ({"source": "test", "per_track": per_track, "unmatched": []}
                    if targets is not None else {"source": "none", "per_track": {}, "unmatched": []}),
    }
