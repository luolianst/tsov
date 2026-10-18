"""Score -> live graph bridge for the tsov-host IPC (S2.2).

The v0 graph is intentionally minimal: existing sidecar tracks are addressed by
index; each track carries notes (seconds), gain, pan and mute. Plugin paths and
state blobs stay in the sidecar manifest until the dynamic graph loader lands.
"""

from __future__ import annotations

from typing import Any

from ..core.score import Score


def score_to_graph(score: Score, *, sample_rate: float = 44100.0) -> dict[str, Any]:
    """Convert a Score into the sidecar's v0 live graph."""
    tracks: list[dict[str, Any]] = []
    for track in score.tracks:
        notes = [
            {
                "start": float(note.start),
                "end": float(note.end),
                "pitch_midi": int(note.pitch_midi),
                "velocity": float(note.velocity),
            }
            for note in track.notes
        ]
        tracks.append(
            {
                "id": track.name,
                "name": track.name,
                "notes": notes,
                "gain": float(track.instrument.volume),
                "pan": float(track.pan),
                "mute": bool(track.mute),
            }
        )
    return {
        "sample_rate": float(sample_rate),
        "tempo": float(score.tempo),
        "time_signature": str(score.time_signature),
        "tracks": tracks,
    }


class LiveBridge:
    """Thin adapter that pushes full Score snapshots through the IPC client."""

    def __init__(self, client: Any) -> None:
        self.client = client

    @property
    def revision(self) -> int:
        return int(getattr(self.client, "revision", 0))

    def sync(self, score: Score) -> dict[str, Any]:
        sample_rate = float((getattr(self.client, "info", {}) or {}).get("sampleRate", 44100.0))
        return self.client.load_graph(score_to_graph(score, sample_rate=sample_rate), revision=self.revision)
