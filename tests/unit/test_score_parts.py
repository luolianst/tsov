"""S3.1：Part schema / 迁移 / 旧平铺 notes 兼容。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from tsov.core.notes import Note
from tsov.core.score import (
    Instrument,
    Part,
    Score,
    Track,
    ensure_parts,
    needs_parts_migration,
)
from tsov.host import Project

from unit._cleanup import rmtree_force


def _note(start, end, pitch=60):
    return Note(start=start, end=end, pitch_midi=pitch, pitch_hz=440.0)


def _track(notes=None):
    return Track(name="melody", instrument=Instrument(program="piano", volume=1.0),
                 notes=notes or [])


def _score(notes=None):
    return Score(title="t", tempo=120.0, tracks=[_track(notes or [])])


def test_ensure_parts_wraps_flat_track_and_is_idempotent():
    score = _score([_note(0.0, 0.5, 60), _note(0.6, 1.0, 62)])
    assert needs_parts_migration(score.to_dict()) is True
    assert ensure_parts(score) is True
    track = score.tracks[0]
    assert len(track.parts) == 1
    part = track.parts[0]
    assert part.id and part.start == 0.0 and part.length == 1.0
    assert part.notes is track.notes  # 默认 Part 与旧平铺共享同一列表
    assert score.meta["schema_version"] == 2
    # 第二次不再改动
    before = score.to_dict()
    assert ensure_parts(score) is False
    assert score.to_dict() == before


def test_part_with_offset_flattens_absolute_notes():
    track = Track(name="p", instrument=Instrument(program="piano"),
                  parts=[Part(id="p1", start=2.0, length=1.0,
                              notes=[_note(0.0, 0.5, 60)])])
    flat = track.flat_notes()
    assert len(flat) == 1 and flat[0].start == 2.0 and flat[0].end == 2.5
    data = track.to_dict()
    assert data["parts"][0]["notes"][0]["start"] == 0.0  # 存 part-local
    assert data["notes"][0]["start"] == 2.0            # 旧读侧仍是绝对时间
    back = Track.from_dict(data)
    assert back.parts[0].start == 2.0
    assert back.parts[0].notes[0].start == 0.0
    assert back.notes[0].start == 2.0


def test_default_part_roundtrip_keeps_shared_notes():
    track = _track([_note(0.0, 0.5, 60)])
    ensure_parts(Score(title="t", tracks=[track]))
    data = track.to_dict()
    back = Track.from_dict(data)
    assert back.parts is not None and len(back.parts) == 1
    assert back.notes == back.parts[0].notes


def test_project_open_migrates_old_score_and_writes_bak(tmp_path):
    root = tmp_path / f"partmig-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    old = _score([_note(0.0, 0.5, 60)]).to_dict()
    old["tracks"][0].pop("parts", None)
    old["meta"] = {"schema_version": 1}
    raw = json.dumps(old, ensure_ascii=False, indent=2)
    (root / "score.json").write_text(raw, encoding="utf-8")
    try:
        project = Project.open(root)
        assert (root / "score.json.bak").read_text(encoding="utf-8") == raw
        assert project.score.meta["schema_version"] == 2
        assert len(project.score.tracks[0].parts) == 1
        assert project.score.tracks[0].parts[0].start == 0.0
        data = json.loads((root / "score.json").read_text(encoding="utf-8"))
        assert data["meta"]["schema_version"] == 2
        assert len(data["tracks"][0]["parts"]) == 1
        assert data["tracks"][0]["notes"][0]["pitch_midi"] == 60
    finally:
        rmtree_force(root)


def test_new_track_gets_default_part():
    from tsov.host.command import EditBatch

    score = _score([_note(0.0, 0.5, 60)])
    ensure_parts(score)
    new, result = EditBatch(label="+track").add("add_track", value={"name": "bass"}).apply(score)
    assert result.ok and len(new.tracks) == 2
    assert new.tracks[1].parts is not None and len(new.tracks[1].parts) == 1
    assert new.tracks[1].parts[0].notes == []
