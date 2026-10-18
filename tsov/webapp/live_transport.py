"""Web -> tsov-host live transport bridge (S2.3).

The web play button is non-blocking: this service starts/reuses a sidecar for
the project, pushes the current Score into its live graph, and streams playhead
state to SSE at ~30 Hz. Projects containing non-VST3 tracks are rejected here
and the playback route falls back to the legacy wav path until those sources
are supported by the sidecar.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..host.host_client import HostClient
from ..host.live import score_to_graph
from .helpers import score_duration
from .state import EventBus


class LiveUnsupported(RuntimeError):
    """Project cannot be played by the current VST3-only sidecar."""


def _resolve_plugin(program: str) -> Path | None:
    if not program.startswith("vst3:"):
        return None
    path = Path(program[len("vst3:"):])
    if path.is_file():
        return path.resolve()
    if path.is_dir():
        for inner in sorted(path.glob("Contents/*/*.vst3")):
            if inner.is_file():
                return inner.resolve()
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveTransport:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._lock = threading.RLock()
        self._client: HostClient | None = None
        self._project = None
        self._project_name = ""
        self._manifest: Path | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: tuple[float, float] | None = None
        self._last_error = ""

    # ---------------- lifecycle ----------------

    def _manifest_for(self, project) -> tuple[Path, list[int]]:
        tracks = []
        selected: list[int] = []
        for index, track in enumerate(project.score.tracks):
            kind = str(getattr(track, "kind", "midi") or "midi")
            if kind != "midi":
                if track.audio_clips():
                    raise LiveUnsupported(f"音频轨暂不支持实时播放：{track.name}")
                continue
            program = str(getattr(track.instrument, "program", "") or "")
            if not program.startswith("vst3:"):
                if track.notes:
                    raise LiveUnsupported(f"非 VST3 轨暂不支持实时播放：{track.name}")
                continue
            plugin = _resolve_plugin(program)
            if plugin is None or not plugin.exists():
                raise LiveUnsupported(f"VST3 插件不存在：{track.name} / {program}")
            item: dict[str, Any] = {
                "name": track.name or f"track {index + 1}",
                "plugin": str(plugin),
                "gain": float(track.instrument.volume),
                "pan": float(track.pan),
                "note": 60,
            }
            preset = getattr(track.instrument, "preset", None) or {}
            if str(preset.get("kind") or "") == "state" and preset.get("path"):
                state = Path(str(preset["path"]))
                if not state.is_absolute():
                    state = project.root / state
                if state.is_file():
                    item["state"] = str(state.resolve())
            tracks.append(item)
            selected.append(index)

        if not selected:
            raise LiveUnsupported("工程没有可实时播放的 VST3 轨")
        return tracks, selected

    def _ensure_client(self, project) -> HostClient:
        if self._client is not None and self._project_name == project.name:
            return self._client
        self._shutdown_client()

        tracks, selected = self._manifest_for(project)
        out_dir = project.root.parent / ".tsov-live"
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = out_dir / f"{project.name}-{uuid.uuid4().hex[:8]}.json"
        manifest.write_text(json.dumps(tracks, ensure_ascii=False, indent=2), encoding="utf-8")

        device = os.environ.get("TSOV_LIVE_DEVICE", "GP-50")
        device_type = os.environ.get("TSOV_LIVE_DEVICE_TYPE", "Exclusive")
        client = HostClient(
            manifest,
            port=_free_port(),
            device=device,
            device_type=device_type,
        )
        try:
            client.start()
            graph = score_to_graph(project.score, sample_rate=float(client.info.get("sampleRate", 44100.0)))
            graph["tracks"] = [graph["tracks"][i] for i in selected]
            client.load_graph(graph, revision=0)
        except Exception:
            client.stop()
            manifest.unlink(missing_ok=True)
            raise

        if len(selected) == len(project.score.tracks):
            project.attach_live(client)
        self._client = client
        self._project = project
        self._project_name = project.name
        self._manifest = manifest
        return client

    def _shutdown_client(self) -> None:
        old = self._client
        old_project = self._project
        self._client = None
        self._project = None
        self._project_name = ""
        self._loop = None
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None
        if old is not None:
            try:
                old.transport("stop")
            except Exception:  # noqa: BLE001
                pass
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                pass
        if old_project is not None and getattr(old_project, "_live_client", None) is old:
            old_project._live_client = None
        if self._manifest is not None:
            self._manifest.unlink(missing_ok=True)
            self._manifest = None

    # ---------------- transport ----------------

    def play(self, project, *, start: float = 0.0, loop: tuple[float, float] | None = None) -> dict:
        with self._lock:
            client = self._ensure_client(project)
            self._loop = loop
            self._stop.clear()
            sample_rate = float(client.info.get("sampleRate", 44100.0))
            position = max(0, int(round(float(start) * sample_rate)))
            client.transport("seek", position=position)
            client.transport("play")
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._stream_playhead,
                    args=(client, project.name, sample_rate),
                    name="tsov-live-playhead",
                    daemon=True,
                )
                self._thread.start()
            duration = score_duration(project.score, project.root)
            payload = {
                "ok": True,
                "mode": "live",
                "duration": duration,
                "start": float(start),
                "loop": list(loop) if loop else None,
            }
            self.bus.publish(project.name, "playback_start", {
                "duration": duration, "start": float(start),
                "loop": list(loop) if loop else None, "mode": "live",
            })
            return payload

    def stop(self, project_name: str = "") -> dict:
        with self._lock:
            self._stop.set()
            client = self._client
            if client is not None:
                try:
                    client.transport("stop")
                except Exception:  # noqa: BLE001
                    pass
            self._loop = None
        if project_name:
            self.bus.publish(project_name, "playback_stop", {"mode": "live"})
        return {"ok": True}

    def seek(self, project_name: str, position: float) -> dict:
        with self._lock:
            client = self._client
            if client is None or self._project_name != project_name:
                return {"ok": False, "reason": "not_started"}
            sample_rate = float(client.info.get("sampleRate", 44100.0))
            client.transport("seek", position=max(0, int(round(float(position) * sample_rate))))
        return {"ok": True}

    def state(self) -> dict:
        with self._lock:
            client = self._client
        if client is None:
            return {"ok": False, "mode": "live"}
        try:
            return {"ok": True, "mode": "live", **client.state()}
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "mode": "live", "error": self._last_error}

    def _stream_playhead(self, client: HostClient, project_name: str, sample_rate: float) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            try:
                state = client.state()
            except Exception as exc:  # noqa: BLE001
                self.bus.publish(project_name, "playback_stop", {
                    "mode": "live", "error": f"{type(exc).__name__}: {exc}",
                })
                break
            if not state.get("playing"):
                self.bus.publish(project_name, "playback_stop", {"mode": "live"})
                break
            playhead = float(state.get("playhead", 0)) / sample_rate
            loop = self._loop
            if loop is not None and playhead >= loop[1]:
                try:
                    client.transport("seek", position=max(0, int(round(loop[0] * sample_rate))))
                except Exception:  # noqa: BLE001
                    pass
                playhead = float(loop[0])
            self.bus.publish(project_name, "playhead", {
                "playhead": round(max(0.0, playhead), 6),
                "playing": True,
                "xruns": int(state.get("xruns", 0)),
                "masterPeak": float(state.get("masterPeak", 0.0)),
            })
            next_tick += 1.0 / 30.0
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()

    def close(self) -> None:
        with self._lock:
            self._shutdown_client()
