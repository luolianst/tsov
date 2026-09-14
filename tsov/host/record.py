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
