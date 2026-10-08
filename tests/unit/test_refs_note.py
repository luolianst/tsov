"""v0.2 批D（P6）单测：build_refs_note —— 【对象引用】块（发送时现场解析）。"""

from __future__ import annotations

from tsov.core.notes import Note
from tsov.core.score import Bus, Effect, Instrument, Score, Track


def _mk_score() -> Score:
    s = Score(title="t", tempo=120.0, tracks=[
        Track(name="melody", pan=-0.25, instrument=Instrument(
            program="violin", volume=1.0,
            effects=[Effect(type="reverb", params={"room_size": 0.5, "wet_level": 0.3})]),
            notes=[
                Note(start=4.5, end=5.0, pitch_midi=67, pitch_hz=392.0, velocity=0.8),
                Note(start=4.75, end=5.0, pitch_midi=69, pitch_hz=440.0, velocity=0.5),
            ]),
        Track(name="bass"),
    ])
    s.buses = [Bus(name="drums", effects=[Effect(type="compressor", params={})])]
    s.master.effects = [Effect(type="limiter", params={"threshold_db": -1.0})]
    return s


def test_refs_note_none_and_empty():
    from tsov.webapp.agent_session import build_refs_note

    assert build_refs_note(_mk_score(), None) == ""
    assert build_refs_note(_mk_score(), []) == ""


def test_refs_note_track():
    from tsov.webapp.agent_session import build_refs_note

    out = build_refs_note(_mk_score(), [{"kind": "track", "track": 0}])
    assert "【对象引用】" in out
    assert "track[0]「melody」" in out and "program='violin'" in out
    assert "音符 2" in out and "vol 1.00" in out and "pan -0.25" in out


def test_refs_note_notes_bar_beat_and_name():
    """tempo 120、4/4：beat=0.5s、小节=2s → 4.5s = bar3.2；67→G4；vel 0.80。"""
    from tsov.webapp.agent_session import build_refs_note

    out = build_refs_note(_mk_score(), [{"kind": "notes", "track": 0, "indices": [0, 1]}])
    assert "bar3.2 G4 vel 0.80" in out
    assert "音符 track[0] [0,1]（2 个）" in out


def test_refs_note_fx_scopes():
    from tsov.webapp.agent_session import build_refs_note

    out = build_refs_note(_mk_score(), [
        {"kind": "fx", "scope": "track", "track": 0, "index": 0},
        {"kind": "fx", "scope": "bus", "ref": "drums", "index": 0},
        {"kind": "fx", "scope": "master", "index": 0},
    ])
    assert "效果器 track[0]「melody」.fx[0] 'reverb'" in out and "room_size 0.5" in out
    assert "bus「drums」.fx[0] 'compressor'" in out
    assert "master.fx[0] 'limiter'" in out


def test_refs_note_invalid_fallback():
    from tsov.webapp.agent_session import build_refs_note

    out = build_refs_note(_mk_score(), [
        {"kind": "track", "track": 9},
        {"kind": "notes", "track": 0, "indices": [0, 7]},
        {"kind": "fx", "scope": "track", "track": 0, "index": 5},
    ])
    assert "引用已失效：轨道 track[9] 不存在" in out
    assert "[7]（已失效）" in out          # 单条失效不阻断整块
    assert "效果器 #5 不存在" in out
    assert "【对象引用】" in out
