"""Score -> live graph bridge for the tsov-host IPC (S2.2/S2.5).

The v0 graph is intentionally minimal: existing sidecar tracks are addressed by
index; each track carries notes (seconds), gain, pan and mute. Plugin paths and
state blobs stay in the sidecar manifest until the dynamic graph loader lands.
``LiveBridge`` sends a full graph when the baseline is unknown or structure
changes, and E1 ``patch`` ops for subsequent note/mix edits.
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
    """Incremental score -> sidecar graph bridge (S2.2/S2.5).

    The first sync (or a structural change: track count/name/tempo/time signature)
    uses a full ``load_graph``. Subsequent note/mix-only edits send E1 ops through
    ``patch`` and advance the server revision without re-sending the whole score.
    """

    def __init__(self, client: Any, score: Score | None = None) -> None:
        self.client = client
        self._last: dict[str, Any] | None = None
        if score is not None and int(getattr(client, "revision", 0)) > 0:
            # The caller has already loaded this score (LiveTransport does this
            # before attach_live); use it as the diff baseline.
            self._last = score_to_graph(
                score,
                sample_rate=float((getattr(client, "info", {}) or {}).get("sampleRate", 44100.0)),
            )

    @property
    def revision(self) -> int:
        return int(getattr(self.client, "revision", 0))

    @staticmethod
    def _ops_between(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]] | None:
        if len(previous.get("tracks", [])) != len(current.get("tracks", [])):
            return None
        if any(previous.get(key) != current.get(key) for key in ("sample_rate", "tempo", "time_signature")):
            return None
        ops: list[dict[str, Any]] = []
        for index, (old, new) in enumerate(zip(previous["tracks"], current["tracks"])):
            if old.get("name") != new.get("name"):
                return None
            if old.get("notes") != new.get("notes"):
                ops.append({"op": "set_notes", "track": index, "notes": new.get("notes", [])})
            if any(old.get(key) != new.get(key) for key in ("gain", "pan", "mute")):
                ops.append({
                    "op": "set_track_mix",
                    "track": index,
                    "gain": new.get("gain", 1.0),
                    "pan": new.get("pan", 0.0),
                    "mute": bool(new.get("mute", False)),
                })
        return ops

    def sync(self, score: Score) -> dict[str, Any]:
        sample_rate = float((getattr(self.client, "info", {}) or {}).get("sampleRate", 44100.0))
        current = score_to_graph(score, sample_rate=sample_rate)

        if self._last is None:
            result = self.client.load_graph(current, revision=self.revision)
            self._last = current
            return result

        ops = self._ops_between(self._last, current)
        if ops is None:
            result = self.client.load_graph(current, revision=self.revision)
            self._last = current
            return result
        if not ops:
            return {"ok": True, "revision": self.revision, "ops": 0}
        result = self.client.patch(ops, revision=self.revision)
        self._last = current
        return result
