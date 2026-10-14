"""P62a 静音体检单测（2026-10-14）：判定三条件 / 地板边界 / MT 策展 hint / stem_peaks 采集。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host.diagnostics import format_warnings_text, scan_silent_instruments

_HAS_SF = Path("vendor/soundfonts/FluidR3_GM.sf2").is_file()

_MT = r"D:\VST3\MT-PowerDrumKit.vst3"


def _note():
    return Note(start=0.0, end=0.5, pitch_midi=36, pitch_hz=73.42, velocity=0.9)


def _track(name="T", program="", notes=None, **kw):
    return Track(name=name, instrument=Instrument(program=program),
                 notes=list(notes) if notes is not None else [_note()], **kw)


def _score(*tracks):
    return Score(tempo=120.0, tracks=list(tracks))


def test_vst3_silent_flagged_with_map_hint():
    s = _score(_track("MT-Drums", f"vst3:{_MT}"))
    w = scan_silent_instruments(s, {0: 0.0})
    assert len(w) == 1
    assert w[0]["code"] == "instrument_silent"
    assert w[0]["track"] == 0 and w[0]["name"] == "MT-Drums"
    assert w[0]["plugin_name"] == "MT-PowerDrumKit"
    assert "START" in w[0]["hint"]          # 策展 meta.notes（真实 v3-map）
    assert w[0]["notes_count"] == 1


def test_loud_vst3_not_flagged():
    s = _score(_track("MT", f"vst3:{_MT}"))
    assert scan_silent_instruments(s, {0: 0.5}) == []


def test_gm_track_zero_not_flagged():
    s = _score(_track("Piano", ""))         # 非 vst3: → 不在检测域
    assert scan_silent_instruments(s, {0: 0.0}) == []


def test_vst3_without_notes_not_flagged():
    s = _score(_track("MT", f"vst3:{_MT}", notes=[]))
    assert scan_silent_instruments(s, {0: 0.0}) == []


def test_floor_boundary():
    s = _score(_track("MT", f"vst3:{_MT}"))
    assert len(scan_silent_instruments(s, {0: 1e-7})) == 1    # ≈ -140 dB → 静音
    assert scan_silent_instruments(s, {0: 1e-5}) == []        # ≈ -100 dB → 有声


def test_unknown_map_gets_default_hint():
    s = _score(_track("X", r"vst3:C:\nope\NoSuchPlugin.vst3"))
    w = scan_silent_instruments(s, {0: 0.0})
    assert len(w) == 1 and "插件界面" in w[0]["hint"]


def test_gated_absent_track_not_flagged():
    s = _score(_track("A", f"vst3:{_MT}"), _track("B", f"vst3:{_MT}"), _track("C", ""))
    # B 缺席（如 mute/solo 门控未采集）→ 只报 A；C 非 vst3 → 不报
    w = scan_silent_instruments(s, {0: 0.0, 2: 0.0})
    assert [x["track"] for x in w] == [0]


def test_format_text():
    s = _score(_track("MT", f"vst3:{_MT}"))
    txt = format_warnings_text(scan_silent_instruments(s, {0: 0.0}))
    assert "MT" in txt and "instrument_silent" in txt
    assert format_warnings_text([]) == ""


@pytest.mark.skipif(not _HAS_SF, reason="无 FluidR3_GM.sf2（渲染用例跳过）")
def test_render_buses_collects_stem_peaks_skips_muted():
    from tsov.host import HostEngine
    from tsov.host.mix import render_buses

    s = _score(_track("A"), _track("B", mute=True))
    eng = HostEngine()
    sess = eng.load(s)
    try:
        stats: dict = {}
        render_buses(sess, samplerate=eng.samplerate, stereo=False, stats=stats)
    finally:
        sess.close()
    peaks = stats.get("stem_peaks") or {}
    assert 0 in peaks and peaks[0] > 1e-4     # 有声轨采集到峰值
    assert 1 not in peaks                     # mute 门控 → 不采集（合法静音天然排除）
