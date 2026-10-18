"""Minimal Python client for the tsov-host sidecar IPC (v0).

Spawns the native host with a mix manifest, connects to its loopback NDJSON
server, and exposes hello/state/transport/shutdown. Restart re-launches the same
manifest so per-track VST3 states are reloaded.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


class HostError(RuntimeError):
    pass


class HostClient:
    def __init__(
        self,
        manifest: str | Path,
        *,
        host: str | Path | None = None,
        port: int = 8765,
        device: str = "GP-50",
        device_type: str = "Exclusive",
        sample_rate: float = 48000.0,
        block_size: int = 128,
        input_channels: int = 0,
        monitor: bool = False,
    ) -> None:
        self.manifest = Path(manifest)
        self.port = port
        self.device = device
        self.device_type = device_type
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.input_channels = int(input_channels)
        self.monitor = bool(monitor)
        self.root = Path(__file__).resolve().parents[2]
        self.host = Path(host) if host else self.root / "native" / "tsov-host" / "build" / "bin" / "Release" / "tsov-host.exe"
        self.proc: subprocess.Popen[str] | None = None
        self.sock: socket.socket | None = None
        self._reader = None
        self._next_id = 0
        self.info: dict[str, Any] = {}
        self.revision = 0
        self._io_lock = threading.Lock()

    def start(self, timeout: float = 15.0) -> dict[str, Any]:
        if not self.manifest.exists():
            raise HostError(f"manifest not found: {self.manifest}")
        cmd = [
            str(self.host),
            "--mix-manifest", str(self.manifest),
            "--serve",
            "--port", str(self.port),
            "--device", self.device,
            "--device-type", self.device_type,
            "--sample-rate", str(self.sample_rate),
            "--block-size", str(self.block_size),
        ]
        if self.input_channels > 0:
            cmd += ["--input-channels", str(self.input_channels)]
        if self.monitor:
            cmd += ["--monitor"]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        deadline = time.monotonic() + timeout
        ready = False
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                output = self.proc.stdout.read() if self.proc.stdout else ""
                raise HostError(f"host exited before IPC ready (code={self.proc.returncode}): {output}")
            line = self.proc.stdout.readline() if self.proc.stdout else ""
            if line.startswith("ipc_ready"):
                ready = True
                break
        if not ready:
            self.stop()
            raise HostError("timeout waiting for ipc_ready")

        while time.monotonic() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=1.0)
                break
            except OSError:
                time.sleep(0.05)
        if self.sock is None:
            self.stop()
            raise HostError("cannot connect to host IPC")
        self._reader = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self.info = self.request("hello", {"protocol": 0, "client": "tsov-python"})
        self.revision = int(self.info.get("revision", 0))
        return self.info

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self.sock is None or self._reader is None:
            raise HostError("host is not connected")
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        with self._io_lock:
            self.sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            line = self._reader.readline()
        if not line:
            raise HostError("host closed the IPC connection")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise HostError(f"unexpected response id: {response}")
        if "error" in response:
            error = response["error"]
            raise HostError(f"{error.get('code')}: {error.get('message')}")
        return response.get("result")

    def state(self) -> dict[str, Any]:
        state = self.request("state")
        self.revision = int(state.get("revision", self.revision))
        return state

    def load_graph(self, graph: dict[str, Any], revision: int | None = None) -> dict[str, Any]:
        """全量装载活图（tracks[].notes/gain/pan/mute）；服务端 revision 单调递增。"""
        expected = self.revision if revision is None else int(revision)
        result = self.request("load_graph", {"revision": expected, "graph": graph})
        self.revision = int(result.get("revision", self.revision))
        return result

    def patch(self, ops: list[dict[str, Any]], revision: int | None = None) -> dict[str, Any]:
        """增量 patch（set_notes / set_track_mix）；revision 断档由服务端拒绝（-32001）。"""
        expected = self.revision if revision is None else int(revision)
        result = self.request("patch", {"revision": expected, "ops": ops})
        self.revision = int(result.get("revision", self.revision))
        return result

    def transport(self, action: str, **params: Any) -> dict[str, Any]:
        return self.request("transport", {"action": action, **params})

    def record_start(self, path: str, channels: int = 1) -> dict[str, Any]:
        return self.request("record_start", {"path": str(path), "channels": int(channels)})

    def record_stop(self) -> dict[str, Any]:
        return self.request("record_stop")

    def record_status(self) -> dict[str, Any]:
        return self.request("record_status")

    def shutdown(self) -> None:
        if self.sock is not None:
            try:
                self.request("shutdown", {"graceful": True})
            except (HostError, OSError):
                pass
        self._close_socket()

    def stop(self) -> None:
        self.shutdown()
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            self.proc = None

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start()

    def _close_socket(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> "HostClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


def _main() -> int:
    parser = argparse.ArgumentParser(description="tsov-host IPC client smoke")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device", default="GP-50")
    parser.add_argument("--device-type", default="Exclusive")
    args = parser.parse_args()

    client = HostClient(args.manifest, host=args.host, port=args.port, device=args.device, device_type=args.device_type)
    try:
        info = client.start()
        print("hello", json.dumps(info, ensure_ascii=False))
        print("play", client.transport("play"))
        time.sleep(args.duration)
        state = client.state()
        print("state", json.dumps(state, ensure_ascii=False))
        print("stop", client.transport("stop"))
    finally:
        client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
