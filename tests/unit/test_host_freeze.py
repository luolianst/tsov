"""P83 显式 freeze：模型/命令/资产/stale/导出/live manifest 回归。"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host.cache import StemStore
from tsov.host.command import EditBatch
from tsov.host.freeze import freeze_state, render_freeze_asset
from tsov.host.project import Project


def _project(tmp_path: Path) -> Project:
    score = Score(
        title="freeze test",
        tempo=120,
        tracks=[Track(
            name="piano",
            instrument=Instrument(program="piano"),
            notes=[Note(start=0.0, end=1.0, pitch_midi=60, pitch_hz=261.63, velocity=0.8)],
        )],
    )
    root = tmp_path / "freeze-test"
    if root.exists():
        shutil.rmtree(root)
    return Project.create("freeze-test", score, parent=tmp_path)


def _freeze(project: Project) -> dict:
    meta = render_freeze_asset(project, 0, samplerate=44100, store=StemStore(project.root))
    batch = EditBatch("freeze")
    batch.add("freeze_track", track=0, value={**meta, "force": True})
    result = project.apply_batch(batch, "freeze test")
    assert result["ok"], result
    return meta


def test_freeze_roundtrip_and_source_lock(tmp_path):
    project = _project(tmp_path)
    meta = _freeze(project)
    track = project.score.tracks[0]
    assert track.freeze["asset_id"] == meta["asset_id"]
    assert StemStore(project.root).has(meta["asset_id"])

    locked = EditBatch("locked").add("set_notes", track=0, value=[])
    result = locked.apply(project.score)[1]
    assert not result.ok
    assert "已冻结" in result.errors[0]

    allowed = EditBatch("mix").add("set_track_mix", track=0, value={"volume": 0.5})
    result = allowed.apply(project.score)[1]
    assert result.ok, result.errors

    # 持久化读回
    project.save()
    reopened = Project.open(project.root)
    assert reopened.score.tracks[0].freeze["content_hash"] == meta["content_hash"]


def test_freeze_stale_after_note_change(tmp_path):
    project = _project(tmp_path)
    _freeze(project)
    project.score.tracks[0].notes[0].end = 2.0
    status = freeze_state(project.score.tracks[0], project.score, StemStore(project.root), 44100)
    assert status["state"] == "stale"


def test_freeze_missing_asset(tmp_path):
    project = _project(tmp_path)
    meta = _freeze(project)
    StemStore(project.root).path_for(meta["asset_id"]).unlink()
    status = freeze_state(project.score.tracks[0], project.score, StemStore(project.root), 44100)
    assert status["state"] == "missing"


def test_frozen_export_skips_source(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _freeze(project)
    from tsov.host import mix as mixmod
    from tsov.host.engine import HostEngine

    def _boom(*args, **kwargs):
        raise AssertionError("frozen track must not instantiate/render source")

    monkeypatch.setattr(mixmod, "render_track_source", _boom)
    engine = HostEngine()
    session = engine.load(project.score, base_dir=project.root)
    try:
        audio = mixmod.render_buses(session, 44100, cache=StemStore(project.root))
    finally:
        session.close()
    assert audio.shape[1] == 2
    assert np.max(np.abs(audio)) > 0.0


def test_unfreeze_clears_state(tmp_path):
    project = _project(tmp_path)
    _freeze(project)
    batch = EditBatch("unfreeze").add("unfreeze_track", track=0)
    result = project.apply_batch(batch, "unfreeze test")
    assert result["ok"], result
    assert project.score.tracks[0].freeze is None
