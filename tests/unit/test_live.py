from __future__ import annotations

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host.command import EditBatch
from tsov.host.live import score_to_graph
from tsov.host.project import Project


class FakeLiveClient:
    info = {"sampleRate": 44100.0}
    revision = 0

    def __init__(self) -> None:
        self.calls = []

    def load_graph(self, graph, revision=None):
        self.calls.append({"graph": graph, "revision": revision})
        self.revision += 1
        return {"ok": True, "revision": self.revision, "tracks": len(graph["tracks"])}


def _score() -> Score:
    return Score(
        tempo=120.0,
        time_signature="4/4",
        tracks=[
            Track(
                name="lead",
                instrument=Instrument(backend="vst3", program="vst3:x", volume=0.5),
                notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.63, velocity=0.8)],
                pan=-0.25,
                mute=True,
            )
        ],
    )


def test_score_to_graph_includes_notes_and_mix():
    graph = score_to_graph(_score(), sample_rate=44100.0)
    assert graph["sample_rate"] == 44100.0
    track = graph["tracks"][0]
    assert track["name"] == "lead"
    assert track["gain"] == 0.5
    assert track["pan"] == -0.25
    assert track["mute"] is True
    assert track["notes"][0]["pitch_midi"] == 60
    assert track["notes"][0]["end"] == 0.5


def test_project_apply_batch_pushes_live_graph(tmp_path):
    project = Project.create("live-test", _score(), parent=tmp_path)
    client = FakeLiveClient()
    project.attach_live(client)

    result = project.apply_batch(EditBatch("mix").add("set_track_mix", track=0, value={"volume": 0.75}))

    assert result["applied"] == 1
    assert result["live"]["ok"] is True
    assert client.revision == 1
    assert client.calls[0]["graph"]["tracks"][0]["gain"] == 0.75
    assert client.calls[0]["revision"] == 0


def test_project_apply_score_pushes_live_graph(tmp_path):
    project = Project.create("live-score-test", _score(), parent=tmp_path)
    client = FakeLiveClient()
    project.attach_live(client)

    new_score = _score()
    new_score.tracks[0].notes[0].pitch_midi = 64
    result = project.apply_score(new_score)

    assert result["ok"] is True
    assert result["live"]["revision"] == 1
    assert client.calls[0]["graph"]["tracks"][0]["notes"][0]["pitch_midi"] == 64
