"""Sidecar record transport (S2.9).

The Web record button can use the same JUCE sidecar that owns playback:
one device, input monitoring, 24-bit WAV.  The legacy Python record path stays
available as a fallback (and for environments where the sidecar cannot open the
selected device).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

from ..host.host_client import HostClient


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _device_name(value) -> str:
    """Front-end sends a sounddevice index; tsov-host matches a device substring."""
    if value is None or value == "":
        try:
            import sounddevice as sd

            index = int(sd.default.device[0])
            if index >= 0:
                name = str(sd.query_devices(index).get("name") or "GP-50")
                return "GP-50" if "GP-50" in name.upper() else name
        except Exception:  # noqa: BLE001
            pass
        return os.environ.get("TSOV_RECORD_DEVICE", "GP-50")
    try:
        index = int(value)
    except (TypeError, ValueError):
        return str(value)
    try:
        import sounddevice as sd

        name = str(sd.query_devices(index).get("name") or "GP-50")
        return "GP-50" if "GP-50" in name.upper() else name
    except Exception:  # noqa: BLE001
        return "GP-50"


class RecordSidecar:
    """Single active record session backed by tsov-host."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._client: HostClient | None = None
        self._manifest: Path | None = None
        self._path: Path | None = None
        self._project = ""
        self._started_at = 0.0
        self._last_error = ""

    @property
    def active(self) -> bool:
        with self._lock:
            return self._client is not None

    def start(self, project, path: str | Path, *, device=None, monitor: bool = True,
              samplerate: int = 44100) -> dict:
        with self._lock:
            if self._client is not None:
                raise RuntimeError("已有 sidecar 录音进行中")
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            out_dir = project.root.parent / ".tsov-record"
            out_dir.mkdir(parents=True, exist_ok=True)
            manifest = out_dir / f"{project.name}-{uuid.uuid4().hex[:8]}.json"
            manifest.write_text("[]", encoding="utf-8")
            client = HostClient(
                manifest,
                port=_free_port(),
                device=_device_name(device),
                device_type=os.environ.get("TSOV_RECORD_DEVICE_TYPE", "Windows Audio"),
                sample_rate=float(samplerate or 44100),
                block_size=int(os.environ.get("TSOV_RECORD_BLOCK_SIZE", "132")),
                input_channels=2,
                monitor=bool(monitor),
            )
            try:
                client.start()
                client.record_start(str(target), 2)
            except Exception:
                try:
                    client.stop()
                finally:
                    manifest.unlink(missing_ok=True)
                raise
            self._client = client
            self._manifest = manifest
            self._path = target
            self._project = project.name
            self._started_at = time.time()
            self._last_error = ""
            return {"path": str(target), "device": _device_name(device), "monitor": bool(monitor)}

    def status(self) -> dict:
        with self._lock:
            client = self._client
            if client is None:
                return {"active": False}
            try:
                state = client.record_status()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                return {"active": True, "error": self._last_error}
            return {
                "active": True,
                **state,
                "elapsed": round(time.time() - self._started_at, 2),
                "path": str(self._path) if self._path else None,
                "project": self._project,
            }

    def stop(self) -> dict:
        with self._lock:
            client = self._client
            if client is None:
                raise RuntimeError("当前没有 sidecar 录音")
            path = self._path
            try:
                result = client.record_stop()
            finally:
                try:
                    client.stop()
                finally:
                    self._client = None
                    if self._manifest is not None:
                        self._manifest.unlink(missing_ok=True)
                    self._manifest = None
            result = dict(result or {})
            result["path"] = str(path) if path else ""
            result["seconds"] = round(int(result.get("recordedFrames", 0)) / 44100.0, 3)
            self._path = None
            self._project = ""
            return result

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            if client is not None:
                try:
                    client.record_stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    client.stop()
                except Exception:  # noqa: BLE001
                    pass
            if self._manifest is not None:
                self._manifest.unlink(missing_ok=True)
                self._manifest = None
            self._path = None
            self._project = ""
