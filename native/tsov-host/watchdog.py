#!/usr/bin/env python3
"""Watchdog for the tsov-host sidecar.

Runs `tsov-host --mix-manifest ...`; if the child exits abnormally, restarts it
with the same manifest so the per-track plugin states are reloaded. Used as the
S1 crash-isolation bridge until the full IPC client (S2) owns lifecycle.

Test-only: --crash-first N adds --crash-after N to the first child, simulating
a plugin crash without touching the manifest.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="tsov-host sidecar watchdog")
    parser.add_argument("--manifest", required=True, help="mix manifest JSON")
    parser.add_argument("--duration", type=float, default=10.0, help="child run duration (seconds)")
    parser.add_argument("--device", default="GP-50")
    parser.add_argument("--device-type", default="Exclusive")
    parser.add_argument("--sample-rate", type=float, default=48000.0)
    parser.add_argument("--block-size", type=int, default=128)
    parser.add_argument("--restart-limit", type=int, default=2)
    parser.add_argument("--crash-first", type=float, default=0.0,
                        help="test-only: crash the first child after N seconds")
    parser.add_argument("--host", default=None, help="tsov-host.exe path")
    parser.add_argument("--log", default=None, help="write watchdog output to this file too")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[2]
    host = Path(args.host) if args.host else root / "native" / "tsov-host" / "build" / "bin" / "Release" / "tsov-host.exe"
    manifest = Path(args.manifest)
    if not manifest.is_absolute():
        manifest = (Path.cwd() / manifest).resolve()
    if not host.exists():
        print(f"[watchdog] host not found: {host}", file=sys.stderr)
        return 2
    if not manifest.exists():
        print(f"[watchdog] manifest not found: {manifest}", file=sys.stderr)
        return 2

    log_file = open(args.log, "w", encoding="utf-8") if args.log else None

    def emit(text: str) -> None:
        print(text, flush=True)
        if log_file is not None:
            log_file.write(text + "\n")
            log_file.flush()

    def child_command(attempt: int) -> list[str]:
        cmd = [
            str(host),
            "--mix-manifest", str(manifest),
            "--device", args.device,
            "--device-type", args.device_type,
            "--duration", str(args.duration),
            "--sample-rate", str(args.sample_rate),
            "--block-size", str(args.block_size),
        ]
        if args.crash_first > 0 and attempt == 1:
            cmd.extend(["--crash-after", str(args.crash_first)])
        return cmd

    restarts = 0
    attempt = 0
    last_exit_at: float | None = None
    while True:
        attempt += 1
        cmd = child_command(attempt)
        started = time.monotonic()
        if last_exit_at is not None:
            emit(f"[watchdog] restart_latency={(started - last_exit_at):.3f}s")
        emit(f"[watchdog] child_start attempt={attempt} crash_first={args.crash_first if attempt == 1 else 0}")
        try:
            result = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(10.0, args.duration + 30.0),
            )
        except subprocess.TimeoutExpired as exc:
            emit(f"[watchdog] child_timeout attempt={attempt}")
            output = (exc.stdout or "") + (exc.stderr or "")
            for line in output.splitlines():
                emit(f"[child] {line}")
            return 1

        elapsed = time.monotonic() - started
        for line in (result.stdout or "").splitlines():
            emit(f"[child] {line}")
        for line in (result.stderr or "").splitlines():
            emit(f"[child] {line}")
        emit(f"[watchdog] child_exit attempt={attempt} code={result.returncode} elapsed={elapsed:.3f}s")
        last_exit_at = time.monotonic()

        if result.returncode == 0:
            emit(f"[watchdog] watchdog=ok restarts={restarts} attempts={attempt}")
            if log_file is not None:
                log_file.close()
            return 0

        if restarts >= args.restart_limit:
            emit(f"[watchdog] watchdog=failed restarts={restarts} attempts={attempt}")
            if log_file is not None:
                log_file.close()
            return 1

        restarts += 1
        emit(f"[watchdog] restart={restarts}")


if __name__ == "__main__":
    raise SystemExit(main())
