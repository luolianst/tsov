"""音频录制（host 层）：输入设备采集 → WAV。

- ``record_to_wav``：从（默认）输入设备采集，``seconds`` 定时停或外部 ``stop_event`` 停；
  纯采集（不做监听混音）；溢出计数进报告
- ``list_input_devices``：可用输入设备清单（CLI ``--device`` 选择用）
- 可注入 ``stream_factory``（测试用假流；缺省 sounddevice.InputStream）
- 低耦合：返回值是报告 dict（路径/时长/帧数/溢出），便于 CLI 与后续转录衔接

CLI：``tsov host record -o out.wav --seconds 5 [--device 名] [--samplerate N] [--channels N] [--then-transcribe]``
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np


def list_input_devices() -> list[dict]:
    """可用输入设备列表：[{index, name, channels, default_samplerate}]。"""
    import sounddevice as sd

    out: list[dict] = []
    default_in = None
    try:
        default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else None
    except Exception:  # pragma: no cover
        pass
    for idx, dev in enumerate(sd.query_devices()):
        if int(dev.get("max_input_channels", 0)) > 0:
            out.append({
                "index": idx,
                "name": str(dev.get("name", "")),
                "channels": int(dev.get("max_input_channels", 0)),
                "default_samplerate": float(dev.get("default_samplerate", 0.0)),
                "default": (idx == default_in),
            })
    return out


def _open_sounddevice_stream(samplerate: int, channels: int, device):
    import sounddevice as sd

    return sd.InputStream(samplerate=samplerate, channels=channels, device=device, dtype="float32")


def record_to_wav(
    path,
    *,
    seconds: float | None = None,
    device=None,
    samplerate: int = 48000,
    channels: int = 1,
    stop_event: threading.Event | None = None,
    stream_factory=None,
    block_frames: int = 1024,
) -> dict:
    """录制到 WAV，返回报告 dict。

    - ``seconds`` 与 ``stop_event`` 至少给一个（否则无停止条件）
    - ``device``：设备索引或名字子串（None = 系统默认输入）
    - ``stream_factory(samplerate, channels, device)`` → 上下文管理器 + ``read(frames)``
      （缺省 sounddevice.InputStream；测试注入假流）
    """
    if seconds is None and stop_event is None:
        raise ValueError("seconds 与 stop_event 至少给一个（否则会一直录下去）")
    if samplerate <= 0 or channels <= 0:
        raise ValueError(f"非法录音参数：samplerate={samplerate} channels={channels}")

    factory = stream_factory or _open_sounddevice_stream
    blocks: list[np.ndarray] = []
    overflowed = False
    done = 0
    target = int(round(float(seconds) * samplerate)) if seconds is not None else None

    with factory(samplerate, channels, device) as stream:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            want = block_frames if target is None else min(block_frames, target - done)
            if want <= 0:
                break
            data, ov = stream.read(want)
            arr = np.asarray(data, dtype=np.float32)
            if arr.ndim == 2 and arr.shape[1] == 1:
                arr = arr[:, 0]
            blocks.append(np.ascontiguousarray(arr))
            done += len(arr)
            overflowed = overflowed or bool(ov)

    audio = np.concatenate(blocks, axis=0) if blocks else np.zeros((0,), dtype=np.float32)

    from .device import write_wav

    path = Path(path)
    if audio.size == 0:
        # soundfile 不接受 0 帧：给 1 帧静音占位（报告里 frames=0 说明实际未采到）
        write_wav(np.zeros((1,), dtype=np.float32), path, samplerate)
    else:
        write_wav(audio, path, samplerate)

    return {
        "path": str(path),
        "frames": int(len(audio)),
        "seconds": round(len(audio) / samplerate, 4),
        "samplerate": int(samplerate),
        "channels": int(channels),
        "device": device,
        "overflowed": bool(overflowed),
    }


# ---------------------------------------------------------------------------
# M-V8 E2 段 3：录音 + 软件监听直通（web 录音面板后端）
# ---------------------------------------------------------------------------


def _open_monitor_stream(samplerate: int, channels: int, device, out_device, monitor: bool):
    """缺省流工厂：monitor=False → InputStream；True → duplex Stream（阻塞 read/write）。"""
    import sounddevice as sd

    if not monitor:
        return sd.InputStream(samplerate=samplerate, channels=channels, device=device, dtype="float32")
    return sd.Stream(samplerate=samplerate, channels=(channels, channels),
                     device=(device, out_device), dtype="float32", blocksize=0)


def record_with_monitor(
    path,
    *,
    seconds: float | None = None,
    device=None,
    out_device=None,
    monitor: bool = False,
    samplerate: int = 48000,
    channels: int = 1,
    stop_event: threading.Event | None = None,
    stream_factory=None,
    block_frames: int = 1024,
    max_seconds: float = 600.0,
) -> dict:
    """录制（可选软件监听直通）→ WAV。返回报告 dict（含 monitor/out_device 字段）。

    - ``monitor=True``：输入实时写回 ``out_device``（软件监听）。延迟 ≈ 输入+输出缓冲
      ——本机 GP-50 实测（output/e2-audio-test/latency_report.json）：DS≈45ms / WASAPI≈54ms / MME≈208ms。
    - ``device``/``out_device``：索引或名字子串（None = 系统默认）。
    - ``max_seconds``：保险上限（防忘记停止长期占用设备）。
    - ``stream_factory(samplerate, channels, device, out_device, monitor)`` → 上下文管理器：
      ``read(frames) -> (data, overflowed)``，monitor=True 时另需 ``write(data)``
      （缺省 `_open_monitor_stream`；测试注入假流）。
    """
    if seconds is None and stop_event is None:
        raise ValueError("seconds 与 stop_event 至少给一个（否则会一直录下去）")
    if samplerate <= 0 or channels <= 0:
        raise ValueError(f"非法录音参数：samplerate={samplerate} channels={channels}")

    factory = stream_factory or _open_monitor_stream
    blocks: list[np.ndarray] = []
    overflowed = False
    done = 0
    target = int(round(float(seconds) * samplerate)) if seconds is not None else None
    deadline = time.monotonic() + float(max_seconds)

    with factory(samplerate, channels, device, out_device, bool(monitor)) as stream:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if time.monotonic() > deadline:
                break
            want = block_frames if target is None else min(block_frames, target - done)
            if want <= 0:
                break
            data, ov = stream.read(want)
            arr = np.asarray(data, dtype=np.float32)
            if arr.ndim == 2 and arr.shape[1] == 1:
                arr = arr[:, 0]
            blocks.append(np.ascontiguousarray(arr))
            done += len(arr)
            overflowed = overflowed or bool(ov)
            if monitor and hasattr(stream, "write"):
                stream.write(arr[:, None] if arr.ndim == 1 else arr)

    audio = np.concatenate(blocks, axis=0) if blocks else np.zeros((0,), dtype=np.float32)

    from .device import write_wav

    path = Path(path)
    if audio.size == 0:
        write_wav(np.zeros((1,), dtype=np.float32), path, samplerate)
    else:
        write_wav(audio, path, samplerate)

    return {
        "path": str(path),
        "frames": int(len(audio)),
        "seconds": round(len(audio) / samplerate, 4),
        "samplerate": int(samplerate),
        "channels": int(channels),
        "device": device,
        "out_device": out_device,
        "monitor": bool(monitor),
        "overflowed": bool(overflowed),
    }
