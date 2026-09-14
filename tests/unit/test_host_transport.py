"""transport 深化测试（里程碑二）：MixStreamer 切片/seek/loop；SynthStreamer 实时调度；Transport 状态机。

离线驱动（不经设备）：`Transport.pull(n)` 直接按块拉——与音频回调同一通路。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host import HostEngine
from tsov.host.transport import MixStreamer, SynthStreamer, Transport

_HAS_SF = Path("vendor/soundfonts/FluidR3_GM.sf2").is_file()


def _sine(freq=440.0, dur=1.0, sr=44100):
    t = np.arange(int(sr * dur)) / sr
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32), sr


def test_mixstreamer_slices_seek_pause_loop():
    audio, sr = _sine()
    tr = Transport(MixStreamer(audio, sr), sr)
    tr.play()
    assert np.allclose(tr.pull(1024), audio[:1024], atol=1e-6)
    # seek → 从新位置切片
    tr.seek(0.5)
    assert np.allclose(tr.pull(512), audio[int(0.5 * sr): int(0.5 * sr) + 512], atol=1e-6)
    # pause → 静音、位置不动
    tr.pause()
    pos = tr.position
    assert not np.any(tr.pull(256))
    assert tr.position == pos
    # loop [0.2, 0.4)：越过尾 → 回环到 loop 头
    tr.play()
    tr.loop = (0.2, 0.4)
    tr.seek(0.39)
    tr.pull(int(0.02 * sr))
    assert 0.19 <= tr.position <= 0.42, tr.position
    # 无 loop → 播完自动 stopped
    tr.loop = None
    tr.seek(tr.duration - 0.001)
    tr.pull(4000)
    assert tr.state == "stopped"


@pytest.mark.skipif(not _HAS_SF, reason="缺 vendor/soundfonts/FluidR3_GM.sf2")
def test_synthstreamer_schedules_notes_and_seek():
    sr = 44100
    notes = [
        Note(start=0.0, end=0.3, pitch_midi=60, pitch_hz=261.63, velocity=0.9),
        Note(start=0.5, end=0.8, pitch_midi=64, pitch_hz=329.63, velocity=0.9),
    ]
    score = Score(
        title="t", tempo=100.0,
        tracks=[Track(name="m", instrument=Instrument(program="piano"), notes=notes)],
    )
    engine = HostEngine()
    session = engine.load(score)
    try:
        tr = Transport(SynthStreamer(session, sr), sr)
        tr.play()
        audio = np.concatenate([tr.pull(1024) for _ in range(int(sr * 1.0 / 1024))])

        def rms(a, b):
            seg = audio[int(a * sr): int(b * sr)]
            return float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)) + 1e-12)

        assert float(np.abs(audio).max()) > 0.01, "应出声"
        assert rms(0.02, 0.28) > rms(0.38, 0.46) * 3, "第一音窗口应显著大于间隙"
        # 第二音 0.5 起音：起音窗能量明显高于其前间隙（钢琴释放尾会抬底噪，留 2× 余量）
        assert rms(0.50, 0.60) > rms(0.42, 0.49) * 2, "第二音起音应可见"
        # 末音结束后迅速归于安静
        assert rms(0.90, 1.10) < rms(0.50, 0.80) / 5, "结束后应安静"
        # seek 到 0.5 → 第二音重新响起
        tr.seek(0.5)
        blk = np.concatenate([tr.pull(1024) for _ in range(int(sr * 0.25 / 1024))])
        assert float(np.abs(blk).max()) > 0.005, "seek 后应出声"
    finally:
        session.close()


@pytest.mark.skipif(not _HAS_SF, reason="缺 vendor/soundfonts/FluidR3_GM.sf2")
def test_engine_stream_falls_back_for_non_streamable():
    """非流式音源（VST3）→ engine.stream 自动回退 MixStreamer 缓冲切片。"""
    dexed = Path("vendor/vst3/Dexed.vst3/Contents/x86_64-win/Dexed.vst3")
    if not dexed.is_file():
        pytest.skip("缺 vendor/vst3/Dexed")
    notes = [Note(start=0.0, end=0.4, pitch_midi=60, pitch_hz=261.63, velocity=0.8)]
    score = Score(
        title="t", tempo=100.0,
        tracks=[Track(name="m", instrument=Instrument(program=f"vst3:{dexed}"), notes=notes)],
    )
    engine = HostEngine()
    session = engine.load(score)
    try:
        streamer = engine.stream(session)
        assert isinstance(streamer.transport.renderer, MixStreamer)
        streamer.transport.play()
        block = streamer.transport.pull(4096)
        assert block.shape == (4096,)
        assert float(np.abs(block).max()) > 0.0, "缓冲切片应含 VST3 渲染内容"
    finally:
        session.close()
