# -*- coding: utf-8 -*-
"""E5 段3 单测：MIDI 导入（tsov/midi/import.py + Project.import_midi）。

- GM 反查 / 元数据（tempo/拍号/调号）/ 未收录号 → piano / is_drum → drums
- 回环：import → export → import，pitch/velocity/program/轨名逐项相等；时间轴 ≤1 tick
  （导出写 220ppq、源 480ppq——首轮换格 ≤1 tick，实测漂移不累积，见 output/e5-verify/seg3_probe.md）
- Project.import_midi：追加非空轨 / 空工程采纳元数据 / 空轨跳过 / 护栏 / 可撤销
"""
from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mido  # noqa: E402
import pretty_midi  # noqa: E402

from tsov.core.notes import Note  # noqa: E402
from tsov.core.score import Instrument, Score, Track  # noqa: E402
from tsov.host import Project  # noqa: E402
from tsov.midi.export import score_to_midi  # noqa: E402

mi = import_module("tsov.midi.import")  # 关键字模块名：经 importlib

TICK96 = 60.0 / (96.0 * 220.0)  # 220ppq @96bpm 的一格等效秒数（导出分辨率）


def _note(start, end, pitch, vel):
    return Note(start=start, end=end, pitch_midi=pitch, pitch_hz=440.0, velocity=vel)


def _mk_source(path: Path) -> Path:
    """造源 MIDI：主奏(guitar 27, 有名) + 低音(bass 33, 无名) + 鼓 + tempo/拍号/调号。"""
    pm = pretty_midi.PrettyMIDI(initial_tempo=96.0, resolution=480)
    pm.time_signature_changes.append(pretty_midi.TimeSignature(3, 4, 0.0))
    pm.key_signature_changes.append(pretty_midi.KeySignature(9, 0.0))  # a major
    lead = pretty_midi.Instrument(program=27, name="lead")
    for i, p in enumerate([69, 71, 73, 76]):
        lead.notes.append(pretty_midi.Note(velocity=80 + i, pitch=p,
                                           start=i * 0.5, end=i * 0.5 + 0.45))
    bass = pretty_midi.Instrument(program=33, name="")
    bass.notes.append(pretty_midi.Note(velocity=100, pitch=45, start=0.0, end=1.2))
    drums = pretty_midi.Instrument(program=0, is_drum=True, name="drums")
    drums.notes.append(pretty_midi.Note(velocity=120, pitch=36, start=0.0, end=0.1))
    pm.instruments += [lead, bass, drums]
    pm.write(str(path))
    return path


def _empty_project(root: Path, name: str = "p") -> Project:
    return Project.create(name, Score(title=name, tracks=[
        Track(name="melody", instrument=Instrument(), notes=[])]), parent=root)


# ---------------- midi_to_score ----------------

def test_meta_and_instruments(tmp_path):
    src = _mk_source(tmp_path / "src.mid")
    sc = mi.midi_to_score(src)
    assert sc.tempo == 96.0
    assert sc.time_signature == "3/4"
    assert [k.key for k in sc.key_candidates] == ["a major"]
    assert [(t.instrument.program, len(t.notes)) for t in sc.tracks] == [
        ("guitar", 4), ("bass", 1), ("drums", 1)]
    # 无名轨 → track N；velocity 0..1（/127）；pitch_hz 对齐
    assert sc.tracks[1].name == "track 2"
    assert abs(sc.tracks[0].notes[0].velocity - 80 / 127) < 1e-9
    assert abs(sc.tracks[0].notes[0].pitch_hz - 440.0) < 1e-6
    assert sc.tracks[0].notes[0].confidence == 1.0


def test_program_name_and_key_name():
    assert mi.program_name(27) == "guitar"      # 同名号取先声明者
    assert mi.program_name(33) == "bass"
    assert mi.program_name(20) == "piano"       # 未收录 → piano（church organ 不在表）
    # pretty_midi 口径：0-11 大调 12-23 小调（12='Cm'、21='Am'——与 key_name_to_key_number 表一致）
    assert mi.key_name(0) == "c major"
    assert mi.key_name(9) == "a major"
    assert mi.key_name(12) == "c minor"
    assert mi.key_name(21) == "a minor"


def test_roundtrip(tmp_path):
    """import → export → import：pitch/velocity/program/轨名逐项相等；时间 ≤1 tick。"""
    src = _mk_source(tmp_path / "src.mid")
    a = mi.midi_to_score(src)
    out = tmp_path / "rt.mid"
    score_to_midi(a, out)
    b = mi.midi_to_score(out)
    assert len(a.tracks) == len(b.tracks)
    for ta, tb in zip(a.tracks, b.tracks):
        assert ta.name == tb.name
        assert ta.instrument.program == tb.instrument.program
        assert len(ta.notes) == len(tb.notes)
        for na, nb in zip(ta.notes, tb.notes):
            assert na.pitch_midi == nb.pitch_midi
            assert abs(na.velocity - nb.velocity) < 1e-9
            assert abs(na.start - nb.start) <= TICK96 + 1e-9
            assert abs(na.end - nb.end) <= TICK96 + 1e-9
    assert a.tempo == b.tempo and a.time_signature == b.time_signature
    assert [k.key for k in a.key_candidates] == [k.key for k in b.key_candidates]


def test_roundtrip_drift_not_cumulative(tmp_path):
    """连续回环 5 轮：帧格换算首轮 ≤1 tick 后固定不漂。"""
    src = _mk_source(tmp_path / "src.mid")
    cur = src
    ends = []
    for k in range(5):
        sc = mi.midi_to_score(cur)
        nxt = tmp_path / f"rt{k}.mid"
        score_to_midi(sc, nxt)
        ends.append(mi.midi_to_score(nxt).tracks[0].notes[0].end)
        cur = nxt
    for e in ends[1:]:
        assert abs(e - ends[1]) < 1e-9     # 第 2 轮起完全一致


# ---------------- Project.import_midi ----------------

def test_import_appends_and_adopts(tmp_path):
    src = _mk_source(tmp_path / "src.mid")
    proj = _empty_project(tmp_path)
    r1 = proj.import_midi(src)
    assert r1["ok"] and r1["added"] == 3 and r1["notes"] == 6 and r1["adopted"] is True
    assert r1["tempo"] == 96.0 and r1["time_signature"] == "3/4"
    assert proj.score.tempo == 96.0 and proj.score.time_signature == "3/4"
    # 无名轨按“追加后位置”命名：lead 有名保名，bass 落在第 3 轨 → track 3
    assert [t.name for t in proj.score.tracks] == ["melody", "lead", "track 3", "drums"]
    # 再导一次：追加、不再采纳元数据、命名接续
    r2 = proj.import_midi(src)
    assert r2["added"] == 3 and r2["adopted"] is False
    assert len(proj.score.tracks) == 7
    assert [t.name for t in proj.score.tracks][-1] == "drums"


def test_import_skips_empty_tracks(tmp_path, monkeypatch):
    """空轨跳过：目前 pretty_midi 读写即丢空轨（防御路径），替身验证。"""
    src = _mk_source(tmp_path / "src.mid")

    def fake(path, *, title=None):  # noqa: ARG001
        return Score(title="x", tempo=100.0, tracks=[
            Track(name="a", instrument=Instrument(program="piano"),
                  notes=[_note(0.0, 0.5, 60, 0.5)]),
            Track(name="ghost", instrument=Instrument(program="piano"), notes=[]),
        ])

    monkeypatch.setattr(mi, "midi_to_score", fake)
    proj = _empty_project(tmp_path)
    r = proj.import_midi(src)
    assert r["added"] == 1 and r["skipped"] == 1 and r["notes"] == 1
    assert [t.name for t in proj.score.tracks] == ["melody", "a"]


def test_import_undoable(tmp_path):
    src = _mk_source(tmp_path / "src.mid")
    proj = _empty_project(tmp_path)
    before = len(proj.score.tracks)
    proj.import_midi(src)
    assert len(proj.score.tracks) == before + 3
    assert proj.undo() is True
    assert len(proj.score.tracks) == before


def test_import_guards(tmp_path):
    proj = _empty_project(tmp_path)
    with pytest.raises(ValueError, match="不存在"):
        proj.import_midi(tmp_path / "nope.mid")
    bad = tmp_path / "x.txt"
    bad.write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持"):
        proj.import_midi(bad)
    empty_ext = tmp_path / "empty.mid"
    empty_ext.write_bytes(b"")
    with pytest.raises(ValueError, match="为空"):
        proj.import_midi(empty_ext)
    # 无音符：mido 造纯 meta 文件
    mid = mido.MidiFile(ticks_per_beat=480)
    t = mido.MidiTrack()
    t.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(96), time=0))
    t.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(t)
    nonotes = tmp_path / "nonotes.mid"
    mid.save(str(nonotes))
    with pytest.raises(ValueError, match="不含任何音符"):
        proj.import_midi(nonotes)
