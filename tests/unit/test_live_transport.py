from __future__ import annotations

from pathlib import Path

import pytest

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.webapp.live_transport import LiveTransport, LiveUnsupported
from tsov.webapp.state import EventBus


class _Project:
    def __init__(self, score: Score, root: Path) -> None:
        self.name = "p"
        self.root = root
        self.score = score


def _note(pitch=60):
    return Note(start=0.0, end=0.5, pitch_midi=pitch, pitch_hz=261.63, velocity=0.8)


def test_manifest_selects_vst3_tracks(tmp_path):
    transport = LiveTransport(EventBus())
    project = _Project(
        Score(tracks=[
            Track(name="lead", instrument=Instrument(backend="vst3", program="vst3:vendor/vst3/Dexed.vst3", volume=0.7),
                  notes=[_note()], pan=0.2),
            Track(name="empty"),
        ]),
        tmp_path,
    )
    tracks, selected = transport._manifest_for(project)
    assert selected == [0]
    assert tracks[0]["name"] == "lead"
    assert tracks[0]["gain"] == 0.7
    assert tracks[0]["pan"] == 0.2
    assert Path(tracks[0]["plugin"]).is_absolute()


def test_manifest_maps_gm_tracks_to_sf2(tmp_path):
    transport = LiveTransport(EventBus())
    project = _Project(
        Score(tracks=[
            Track(name="piano", instrument=Instrument(program="piano"), notes=[_note()]),
            Track(name="drums", instrument=Instrument(program="drums"), notes=[_note(36)]),
        ]),
        tmp_path,
    )
    tracks, selected = transport._manifest_for(project)
    assert selected == [0, 1]
    assert tracks[0]["type"] == "sf2"
    assert tracks[0]["bank"] == 0
    assert tracks[0]["program"] == 0
    assert Path(tracks[0]["soundfont"]).is_file()
    assert tracks[1]["bank"] == 128


def test_manifest_rejects_unknown_program_with_notes(tmp_path):
    transport = LiveTransport(EventBus())
    project = _Project(
        Score(tracks=[Track(name="odd", instrument=Instrument(program="theremin"), notes=[_note()])]),
        tmp_path,
    )
    with pytest.raises(LiveUnsupported):
        transport._manifest_for(project)
