"""录制（host/record.py）单测：假流注入，不依赖真实音频设备。"""

from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tsov.host.record import record_to_wav

SR = 16000


class _FakeStream:
    """假输入流：生成 440Hz 正弦；可配置溢出标记与「第 N 次 read 后触发 stop_event」。"""

    def __init__(self, samplerate, channels, device, *, overflow_at=None, stop_event=None, stop_after=None):
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.t = 0
        self.calls = 0
        self.overflow_at = overflow_at
        self.stop_event = stop_event
        self.stop_after = stop_after
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def read(self, frames):
        t = np.arange(self.t, self.t + frames) / self.samplerate
        self.t += frames
        data = (0.1 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        if self.channels > 1:
            data = np.stack([data] * self.channels, axis=1)
        self.calls += 1
        ov = (self.overflow_at is not None and self.calls >= self.overflow_at)
        if self.stop_event is not None and self.stop_after is not None and self.calls >= self.stop_after:
            self.stop_event.set()
        return data, ov


def _out_dir() -> Path:
    return Path("output") / f"rectest-{uuid.uuid4().hex[:8]}"


def test_record_seconds_writes_wav():
    out = _out_dir()
    try:
        path = out / "rec.wav"
        report = record_to_wav(path, seconds=1.0, samplerate=SR, stream_factory=_FakeStream)
        assert report["frames"] == SR and report["seconds"] == 1.0
        assert report["channels"] == 1 and report["overflowed"] is False
        data, sr = sf.read(str(path))
        assert sr == SR and len(data) == SR and data.ndim == 1
        assert np.abs(data).max() > 0.01
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_record_stop_event_stops():
    out = _out_dir()
    try:
        ev = threading.Event()
        factory = lambda sr, ch, dev: _FakeStream(sr, ch, dev, stop_event=ev, stop_after=3)
        report = record_to_wav(out / "rec.wav", stop_event=ev, samplerate=SR, stream_factory=factory)
        assert report["frames"] == 3 * 1024
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_record_stereo_channels():
    out = _out_dir()
    try:
        report = record_to_wav(out / "rec.wav", seconds=0.25, samplerate=SR, channels=2, stream_factory=_FakeStream)
        assert report["channels"] == 2
        data, sr = sf.read(str(out / "rec.wav"))
        assert data.ndim == 2 and data.shape[1] == 2
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_record_requires_stop_condition():
    with pytest.raises(ValueError):
        record_to_wav("output/x.wav", samplerate=SR, stream_factory=_FakeStream)


def test_record_overflow_flag_propagates():
    out = _out_dir()
    try:
        factory = lambda sr, ch, dev: _FakeStream(sr, ch, dev, overflow_at=2)
        report = record_to_wav(out / "rec.wav", seconds=0.25, samplerate=SR, stream_factory=factory)
        assert report["overflowed"] is True
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_record_zero_seconds_gives_placeholder_wav():
    out = _out_dir()
    try:
        report = record_to_wav(out / "rec.wav", seconds=0.0, samplerate=SR, stream_factory=_FakeStream)
        assert report["frames"] == 0
        data, sr = sf.read(str(out / "rec.wav"))
        assert len(data) == 1  # 1 帧静音占位
    finally:
        shutil.rmtree(out, ignore_errors=True)
