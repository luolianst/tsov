from __future__ import annotations

import json
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

    def attach_live(self, client) -> None:
        self._live_client = client


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


def test_manifest_uses_track_soundfont_override(tmp_path):
    sf2 = tmp_path / "custom.sf2"
    sf2.write_bytes(b"fake-sf2")
    transport = LiveTransport(EventBus())
    project = _Project(
        Score(tracks=[
            Track(name="piano", instrument=Instrument(program="piano", soundfont=str(sf2)),
                  notes=[_note()]),
        ]),
        tmp_path,
    )
    tracks, _selected = transport._manifest_for(project)
    assert tracks[0]["soundfont"] == str(sf2)


def test_manifest_rejects_vst2_with_clear_hint(tmp_path):
    transport = LiveTransport(EventBus())
    project = _Project(
        Score(tracks=[
            Track(name="old", instrument=Instrument(program="vst2:legacy.dll"), notes=[_note()]),
        ]),
        tmp_path,
    )
    with pytest.raises(LiveUnsupported, match="VST2.*不支持实时"):
        transport._manifest_for(project)


def test_manifest_rejects_unknown_program_with_notes(tmp_path):
    transport = LiveTransport(EventBus())
    project = _Project(
        Score(tracks=[Track(name="odd", instrument=Instrument(program="theremin"), notes=[_note()])]),
        tmp_path,
    )
    with pytest.raises(LiveUnsupported):
        transport._manifest_for(project)


class _ReloadClient:
    info = {"sampleRate": 44100.0}
    revision = 1

    def __init__(self):
        self.restarted = False
        self.loaded = None
        self.transports = []

    def state(self):
        return {"playing": False, "playhead": 0}

    def restart(self):
        self.restarted = True
        self.revision = 0
        return {"sampleRate": 44100.0}

    def load_graph(self, graph, revision=0):
        self.loaded = graph
        self.revision = 1
        return {"ok": True, "revision": 1}

    def transport(self, action, **params):
        self.transports.append((action, params))
        return {"ok": True}


def test_reload_editor_state_updates_manifest_and_restarts(tmp_path):
    plugin = tmp_path / "A.vst3"
    plugin.write_bytes(b"plugin")
    state = tmp_path / "A.state"
    state.write_bytes(b"state")
    project = _Project(
        Score(tracks=[Track(name="lead", instrument=Instrument(program=f"vst3:{plugin}"),
                            notes=[_note()])]),
        tmp_path,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"name": "lead", "plugin": str(plugin), "state": ""}]),
                        encoding="utf-8")
    client = _ReloadClient()
    transport = LiveTransport(EventBus())
    transport._client = client
    transport._project = project
    transport._project_name = "p"
    transport._manifest = manifest
    transport._selected = [0]

    result = transport.reload_editor_state("p", str(plugin), str(state))

    assert result["ok"] is True
    assert client.restarted is True
    assert client.loaded is not None
    assert json.loads(manifest.read_text(encoding="utf-8"))[0]["state"] == str(state.resolve())
