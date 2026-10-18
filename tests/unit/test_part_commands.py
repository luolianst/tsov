"""S3.2：Part 命令族（add/move/copy/resize/delete/cross-track）。"""

from __future__ import annotations

import uuid
from pathlib import Path

from tsov.core.notes import Note
from tsov.core.score import Instrument, Part, Score, Track
from tsov.host import EditBatch, Project

from unit._cleanup import rmtree_force


def _note(start, end, pitch=60):
    return Note(start=start, end=end, pitch_midi=pitch, pitch_hz=440.0)


def _track(parts=None, notes=None, name="melody"):
    return Track(name=name, instrument=Instrument(program="piano"),
                 notes=notes or [], parts=parts)


def _score(track=None):
    return Score(title="t", tempo=120.0, tracks=[track or _track(parts=[])])


def _batch(score, op, *, track=0, value):
    batch = EditBatch(label=op).add(op, track=track, value=value)
    return batch.apply(score)


def test_add_part_local_notes_and_flat_view():
    score = _score()
    new, res = _batch(score, "add_part", value={
        "part": {"id": "pA", "start": 2.0, "length": 1.0,
                 "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 60},
                           {"start": 0.5, "end": 1.0, "pitch_midi": 62}]},
    })
    assert res.ok, res.errors
    track = new.tracks[0]
    assert [p.id for p in track.parts] == ["pA"]
    assert [n.start for n in track.notes] == [2.0, 2.5]
    assert track.parts[0].notes[0].start == 0.0


def test_move_part_shifts_flat_notes():
    score = _score(_track(parts=[Part(id="pA", start=1.0, length=1.0,
                                     notes=[_note(0.0, 0.5)])]))
    new, res = _batch(score, "move_part", value={"part": "pA", "start": 3.0})
    assert res.ok, res.errors
    track = new.tracks[0]
    assert track.parts[0].start == 3.0
    assert track.notes[0].start == 3.0


def test_copy_part_creates_independent_notes():
    score = _score(_track(parts=[Part(id="pA", start=1.0, length=1.0,
                                     notes=[_note(0.0, 0.5)])]))
    new, res = _batch(score, "copy_part", value={"part": "pA", "start": 4.0})
    assert res.ok, res.errors
    track = new.tracks[0]
    assert len(track.parts) == 2
    assert {p.start for p in track.parts} == {1.0, 4.0}
    assert len(track.notes) == 2
    # 改副本不得影响源 Part
    track.parts[1].notes[0].pitch_midi = 72
    assert track.parts[0].notes[0].pitch_midi == 60


def test_resize_part_loop_extension_and_crop():
    score = _score(_track(parts=[Part(id="pA", start=0.0, length=1.0,
                                     notes=[_note(0.0, 0.5)])]))
    new, res = _batch(score, "resize_part", value={"part": "pA", "length": 2.5})
    assert res.ok, res.errors
    track = new.tracks[0]
    assert [(round(n.start, 3), round(n.end, 3)) for n in track.notes] == [
        (0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 2.5),
    ]
    assert track.parts[0].length == 2.5
    # 再裁剪回 0.3
    cropped, res2 = _batch(new, "resize_part", value={"part": "pA", "length": 0.3})
    assert res2.ok, res2.errors
    assert len(cropped.tracks[0].notes) == 1
    assert cropped.tracks[0].notes[0].end == 0.3


def test_resize_part_crop_extension_preserves_length():
    """关闭音符循环时右拉延伸：只加长 Part，不复制音符（回弹回归）。"""
    score = _score(_track(parts=[Part(id="pA", start=0.0, length=1.0,
                                     notes=[_note(0.0, 0.5)])]))
    new, res = _batch(score, "resize_part",
                      value={"part": "pA", "length": 3.0, "mode": "crop"})
    assert res.ok, res.errors
    track = new.tracks[0]
    assert track.parts[0].length == 3.0
    assert len(track.parts[0].notes) == 1
    assert [(round(n.start, 3), round(n.end, 3)) for n in track.notes] == [(0.0, 0.5)]


def test_resize_part_left_crop_shifts_notes():
    score = _score(_track(parts=[Part(id="pA", start=1.0, length=2.0,
                                     notes=[_note(0.0, 0.5), _note(1.0, 1.5)])]))
    new, res = _batch(score, "resize_part",
                      value={"part": "pA", "start": 1.4, "length": 1.6})
    assert res.ok, res.errors
    track = new.tracks[0]
    assert track.parts[0].start == 1.4
    assert [(round(n.start, 3), round(n.end, 3)) for n in track.notes] == [
        (1.4, 1.5),  # 原 1.0–1.5 被左边界裁到 1.4–1.5
        (2.0, 2.5),  # 原 2.0–2.5 保持
    ]


def test_delete_part_clears_flat_notes():
    score = _score(_track(parts=[Part(id="pA", start=0.0, length=1.0,
                                     notes=[_note(0.0, 0.5)])]))
    new, res = _batch(score, "delete_part", value={"part": "pA"})
    assert res.ok, res.errors
    assert new.tracks[0].parts == []
    assert new.tracks[0].notes == []


def test_set_notes_part_aware_is_local():
    score = _score(_track(parts=[Part(id="pA", start=2.0, length=1.0,
                                     notes=[_note(0.0, 0.5)])]))
    new, res = _batch(score, "set_notes", value={
        "part": "pA", "notes": [{"start": 0.0, "end": 0.25, "pitch_midi": 67}]})
    assert res.ok, res.errors
    track = new.tracks[0]
    assert track.parts[0].notes[0].start == 0.0
    assert track.notes[0].start == 2.0
    assert track.notes[0].pitch_midi == 67


def test_move_part_to_another_track():
    source = _track(parts=[Part(id="pA", start=1.0, length=1.0,
                                notes=[_note(0.0, 0.5)])], name="src")
    target = _track(parts=[], name="dst")
    score = Score(title="t", tracks=[source, target])
    new, res = _batch(score, "move_part_to_track", track=0,
                      value={"part": "pA", "target_track": 1, "start": 2.0})
    assert res.ok, res.errors
    assert new.tracks[0].parts == [] and new.tracks[0].notes == []
    assert len(new.tracks[1].parts) == 1
    assert new.tracks[1].parts[0].start == 2.0
    assert new.tracks[1].notes[0].start == 2.0


def test_part_ops_undo_redo_roundtrip(tmp_path):
    root = Path(tmp_path) / f"partcmd-{uuid.uuid4().hex[:8]}"
    try:
        p = Project.create("p", _score(_track(parts=[Part(id="pA", start=0.0, length=1.0,
                                                         notes=[_note(0.0, 0.5)])])), parent=root)
        out = p.apply_batch(EditBatch(label="add").add(
            "add_part", value={"part": {"id": "pB", "start": 2.0, "length": 1.0,
                                        "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 64}]}}))
        assert out["ok"] and len(p.score.tracks[0].parts) == 2
        assert p.undo()
        assert [part.id for part in p.score.tracks[0].parts] == ["pA"]
        assert p.redo()
        assert [part.id for part in p.score.tracks[0].parts] == ["pA", "pB"]
    finally:
        rmtree_force(root)


def test_part_flat_notes_drive_midi_export(tmp_path):
    import pretty_midi
    from tsov.midi.export import score_to_midi

    score = _score(_track(parts=[Part(id="pA", start=2.0, length=1.0,
                                     notes=[_note(0.0, 0.5, 67)])]))
    out = tmp_path / "part.mid"
    score_to_midi(score, str(out))
    midi = pretty_midi.PrettyMIDI(str(out))
    notes = [n for inst in midi.instruments for n in inst.notes]
    assert len(notes) == 1
    assert notes[0].pitch == 67
    assert abs(notes[0].start - 2.0) < 1e-6
    assert abs(notes[0].end - 2.5) < 1e-6
