"""Web -> tsov-host live transport bridge.

The web play button is non-blocking: this service starts/reuses a sidecar for
the project, pushes the current Score into its live graph, and streams playhead
state to SSE at ~30 Hz. VST3 tracks and the bundled GM SoundFont are supported;
projects containing unsupported sources still fall back to the legacy wav path.
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
from ..host.vst_worker import editor_state_path
from ..midi.export import DRUM_BANK, GM_PROGRAMS
from .helpers import score_duration
from .state import EventBus

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SF2 = _REPO_ROOT / "vendor" / "soundfonts" / "FluidR3_GM.sf2"
_DEFAULT_SFIZZ = Path("D:/VST3/sfizz.vst3")


class LiveUnsupported(RuntimeError):
    """Project cannot be played by the current sidecar."""


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


def _gm_program_number(program: str) -> int | None:
    if program == "":
        return 0
    if program.isdigit():
        return int(program)
    if program == "drums":
        return 0
    return GM_PROGRAMS.get(program)


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
        self._manifest_tracks: list[dict[str, Any]] = []
        self._selected: list[int] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: tuple[float, float] | None = None
        self._last_error = ""
        self._device_type = ""

    # ---------------- lifecycle ----------------

    def _manifest_for(self, project) -> tuple[list[dict[str, Any]], list[int]]:
        tracks: list[dict[str, Any]] = []
        selected: list[int] = []
        for index, track in enumerate(project.score.tracks):
            kind = str(getattr(track, "kind", "midi") or "midi")
            if kind != "midi":
                if track.audio_clips():
                    raise LiveUnsupported(f"音频轨暂不支持实时播放：{track.name}")
                continue
            if not track.notes:
                continue
            program = str(getattr(track.instrument, "program", "") or "")
            preset = getattr(track.instrument, "preset", None) or {}
            state_path = None
            if str(preset.get("kind") or "") == "state" and preset.get("path"):
                state = Path(str(preset["path"]))
                if not state.is_absolute():
                    state = project.root / state
                if state.is_file():
                    state_path = str(state.resolve())

            item: dict[str, Any] = {
                "name": track.name or f"track {index + 1}",
                "gain": float(track.instrument.volume),
                "pan": float(track.pan),
                "note": 60,
            }

            if program.startswith("vst3:"):
                plugin = _resolve_plugin(program)
                if plugin is None or not plugin.exists():
                    raise LiveUnsupported(f"VST3 插件不存在：{track.name} / {program}")
                if state_path is None:
                    saved = editor_state_path(str(plugin))
                    if saved.is_file():
                        state_path = str(saved)
                item.update({"type": "vst3", "plugin": str(plugin)})
                if state_path:
                    item["state"] = state_path
            elif program.startswith("vst2:") or program.lower().endswith(".dll"):
                raise LiveUnsupported(
                    f"VST2 轨不支持实时播放（数据保留，可离线导出）：{track.name} / {program}")
            elif program.startswith("sfz:"):
                plugin = _resolve_plugin(f"vst3:{_DEFAULT_SFIZZ}")
                if plugin is None or not plugin.exists():
                    raise LiveUnsupported("sfizz VST3 不可用，无法实时播放 SFZ 轨")
                if state_path is None:
                    saved = editor_state_path(str(plugin))
                    if saved.is_file():
                        state_path = str(saved)
                if state_path is None:
                    raise LiveUnsupported(f"SFZ 轨缺少 sfizz 状态：{track.name}")
                item.update({"type": "vst3", "plugin": str(plugin), "state": state_path})
            else:
                program_number = _gm_program_number(program)
                if program_number is None:
                    if track.notes:
                        raise LiveUnsupported(f"未知音色程序，暂不支持实时播放：{track.name} / {program}")
                    continue
                sf2_path = Path(str(getattr(track.instrument, "soundfont", "") or _DEFAULT_SF2))
                if not sf2_path.is_absolute():
                    sf2_path = project.root / sf2_path
                if not sf2_path.is_file():
                    raise LiveUnsupported(f"轨级 SoundFont 不存在：{track.name} / {sf2_path}")
                item.update(
                    {
                        "type": "sf2",
                        "soundfont": str(sf2_path),
                        "bank": DRUM_BANK if program == "drums" else 0,
                        "program": program_number,
                    }
                )

            tracks.append(item)
            selected.append(index)

        if not selected:
            raise LiveUnsupported("工程没有可实时播放的 MIDI 轨")
        return tracks, selected

    def _device_types(self) -> list[str]:
        """首选设备类型 + 可选共享模式兜底（S6：独占被浏览器/其他流占用时不静默失败）。

        默认 Exclusive；`TSOV_LIVE_DEVICE_FALLBACK=0` 可禁用兜底。兜底只在启动失败时尝试，
        成功后 `play` payload 带 `device_type`，前端提示「共享模式」。
        """
        primary = os.environ.get("TSOV_LIVE_DEVICE_TYPE", "Exclusive")
        fallback = os.environ.get("TSOV_LIVE_DEVICE_FALLBACK", "Windows Audio")
        out = [primary]
        if fallback and fallback != primary and os.environ.get("TSOV_LIVE_DEVICE_FALLBACK", "") != "0":
            out.append(fallback)
        return out

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
        sample_rate = float(os.environ.get("TSOV_LIVE_SAMPLE_RATE", "44100"))
        block_size = int(os.environ.get("TSOV_LIVE_BLOCK_SIZE", "132"))
        last_exc: Exception | None = None
        client = None
        used_type = ""
        for device_type in self._device_types():
            client = HostClient(
                manifest,
                port=_free_port(),
                device=device,
                device_type=device_type,
                sample_rate=sample_rate,
                block_size=block_size,
            )
            try:
                client.start()
                graph = score_to_graph(project.score, sample_rate=float(client.info.get("sampleRate", 44100.0)))
                graph["tracks"] = [graph["tracks"][i] for i in selected]
                client.load_graph(graph, revision=0)
                used_type = device_type
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 —— 换设备类型重试；最终仍失败再抛原异常
                last_exc = exc
                try:
                    client.stop()
                except Exception:  # noqa: BLE001
                    pass
                client = None
        if client is None or last_exc is not None:
            manifest.unlink(missing_ok=True)
            raise last_exc or RuntimeError("live sidecar 启动失败")

        self._device_type = used_type
        if len(self._device_types()) > 1 and used_type != self._device_types()[0]:
            self._last_error = f"独占设备不可用，已回退 {used_type}（共享模式）"
        if len(selected) == len(project.score.tracks):
            project.attach_live(client)
        self._client = client
        self._project = project
        self._project_name = project.name
        self._manifest = manifest
        self._manifest_tracks = tracks
        self._selected = selected
        return client

    def _shutdown_client(self) -> None:
        old = self._client
        old_project = self._project
        self._client = None
        self._project = None
        self._project_name = ""
        self._manifest_tracks = []
        self._selected = []
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
            loop_start = int(round(loop[0] * sample_rate)) if loop else 0
            loop_end = int(round(loop[1] * sample_rate)) if loop else 0
            client.transport("loop", start=loop_start, end=loop_end)
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
            latency_samples = int(client.info.get("outputLatencySamples", 0) or 0)
            payload = {
                "ok": True,
                "mode": "live",
                "duration": duration,
                "start": float(start),
                "loop": list(loop) if loop else None,
                "outputLatencySamples": latency_samples,
                "device_type": self._device_type,
                "device_fallback": bool(self._device_type and self._device_type != self._device_types()[0]),
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
            self._loop = None
        if client is not None:
            # S6：host 卡死时 stop 不能把 Web 请求一起拖住——发送线程限时 1s，超时丢弃。
            def _send() -> None:
                try:
                    client.transport("stop")
                except Exception:  # noqa: BLE001
                    pass

            th = threading.Thread(target=_send, name="tsov-live-stop", daemon=True)
            th.start()
            th.join(timeout=1.0)
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

    def reload_editor_state(self, project_name: str, plugin: str, state_path: str) -> dict:
        """G1：编辑器关窗 state → 更新 manifest 并重启 sidecar 重新加载。

        编辑器仍由 Python vst_worker 持有（P62b）；sidecar 是独立插件实例，
        因此关窗保存 state 后必须重启 sidecar 才能听到同一插件状态。
        """
        with self._lock:
            client = self._client
            if client is None or self._project_name != project_name:
                return {"ok": False, "reason": "not_started"}
            source = Path(state_path)
            if not source.is_file():
                return {"ok": False, "reason": "state_missing", "state_path": str(source)}
            if self._manifest is None or not self._manifest.is_file():
                return {"ok": False, "reason": "manifest_missing"}

            tracks = json.loads(self._manifest.read_text(encoding="utf-8"))
            target = Path(plugin)
            targets = {str(target.resolve())}
            if target.is_dir():
                targets |= {str(p.resolve()) for p in target.glob("Contents/*/*.vst3") if p.is_file()}
            changed = False
            for item in tracks:
                candidate = item.get("plugin")
                if not candidate:
                    continue
                if str(Path(str(candidate)).resolve()) in targets:
                    item["state"] = str(source.resolve())
                    changed = True
            if not changed:
                return {"ok": False, "reason": "track_not_found", "plugin": plugin}
            self._manifest.write_text(json.dumps(tracks, ensure_ascii=False, indent=2), encoding="utf-8")
            self._manifest_tracks = tracks

            try:
                previous = client.state()
                was_playing = bool(previous.get("playing"))
                playhead = int(previous.get("playhead", 0) or 0)
            except Exception:  # noqa: BLE001
                was_playing, playhead = False, 0

            try:
                client.restart()
                graph = score_to_graph(
                    self._project.score,
                    sample_rate=float(client.info.get("sampleRate", 44100.0)),
                )
                graph["tracks"] = [graph["tracks"][index] for index in self._selected]
                client.load_graph(graph, revision=0)
                loop = self._loop
                if loop is not None:
                    sample_rate = float(client.info.get("sampleRate", 44100.0))
                    client.transport("loop", start=int(round(loop[0] * sample_rate)),
                                     end=int(round(loop[1] * sample_rate)))
                if was_playing:
                    client.transport("seek", position=max(0, playhead))
                    client.transport("play")
                self._project.attach_live(client)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                return {"ok": False, "reason": "restart_failed", "error": self._last_error}

            return {"ok": True, "reloaded": True, "revision": int(getattr(client, "revision", 0)),
                    "playing": was_playing, "state_path": str(source.resolve())}

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
            latency = float(state.get("outputLatencySamples", 0) or 0) / sample_rate
            # The speaker is hearing this far behind the source clock; expose the
            # audible position so the playhead lines up with what the user hears.
            playhead = max(0.0, playhead - latency)
            loop = self._loop
            if loop is not None and playhead >= loop[1]:
                # The host loops at sample boundaries; this only guards a host
                # that reports a stale playhead just past the loop end.
                playhead = float(loop[0])
            self.bus.publish(project_name, "playhead", {
                "playhead": round(max(0.0, playhead), 6),
                "playing": True,
                "xruns": int(state.get("xruns", 0)),
                "masterPeak": float(state.get("masterPeak", 0.0)),
                "outputLatencySamples": int(state.get("outputLatencySamples", 0) or 0),
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
