"""效果链（host/effect.py）单测：确定性 / 尾巴 / 参数处理 / 混音层集成。

用假音源（短促音符 + 静默区）验证 reverb 尾巴；低采样率加速。
"""

from __future__ import annotations

import numpy as np
import pytest

from tsov.core.notes import Note
from tsov.core.score import Bus, Effect, Instrument, Score, Track
from tsov.host import HostSession, HostTrack, render_buses
from tsov.host.effect import (
    EffectChain,
    apply_effect_chain,
    effect_kinds,
    effect_tail_seconds,
    validate_effect,
)
from tsov.host.instrument import SoundSource

SR = 8000


class _OneShotSource(SoundSource):
    """前 note_len 秒给衰减正弦（模拟一个音符），其余静默（便于观察尾巴）。"""

    def __init__(self, note_len: float = 0.3, freq: float = 440.0):
        self.note_len = float(note_len)
        self.freq = float(freq)

    def render(self, notes, samplerate, n_frames):
        buf = np.zeros(n_frames, dtype=np.float32)
        n = min(n_frames, int(self.note_len * samplerate))
        t = np.arange(n) / samplerate
        buf[:n] = (0.6 * np.sin(2 * np.pi * self.freq * t) * np.exp(-4.0 * t)).astype(np.float32)
        return buf


def _buf(seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (0.8 * np.sin(2 * np.pi * 440 * t) * np.exp(-3.0 * t)).astype(np.float32)


# ------------------------------------------------------------------
# 基础行为
# ------------------------------------------------------------------


def test_gain_scales_exactly_and_keeps_shape():
    buf = _buf()
    out = apply_effect_chain(buf, SR, [Effect(type="gain", params={"gain_db": 6.0})])
    assert out.shape == buf.shape and out.dtype == np.float32
    np.testing.assert_allclose(out, buf * (10 ** (6.0 / 20.0)), rtol=1e-3, atol=1e-5)


def test_empty_chain_is_passthrough():
    buf = _buf()
    assert apply_effect_chain(buf, SR, []) is buf
    assert apply_effect_chain(buf, SR, None) is buf


def test_reverb_tail_exists_and_is_deterministic():
    n = SR  # 1s 缓冲：前 0.25s 一个衰减音，其余静默
    t = np.arange(int(0.25 * SR)) / SR
    buf = np.zeros(n, dtype=np.float32)
    buf[: len(t)] = (0.6 * np.sin(2 * np.pi * 440 * t) * np.exp(-4.0 * t)).astype(np.float32)
    fx = [Effect(type="reverb", params={"room_size": 0.8, "damping": 0.4, "wet_level": 0.5, "dry_level": 0.6})]
    a = apply_effect_chain(buf, SR, fx)
    b = apply_effect_chain(buf, SR, fx)
    np.testing.assert_allclose(a, b, rtol=0, atol=0)  # 逐样本一致（无随机性）
    half = n // 2
    assert np.abs(buf[half:]).max() == 0.0      # 干声后段已静默
    assert np.abs(a[half:]).max() > 1e-3        # 混响尾巴仍有余能


def test_compressor_reduces_peak():
    t = np.arange(SR) / SR
    loud = (0.9 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    out = apply_effect_chain(
        loud, SR,
        [Effect(type="compressor", params={"threshold_db": -20.0, "ratio": 8.0, "attack_ms": 5.0, "release_ms": 120.0})],
    )
    assert np.abs(out).max() < np.abs(loud).max() * 0.95


def test_chain_order_matters():
    buf = _buf()
    a = apply_effect_chain(buf, SR, [Effect(type="gain", params={"gain_db": -12}), Effect(type="distortion", params={"drive_db": 12})])
    b = apply_effect_chain(buf, SR, [Effect(type="distortion", params={"drive_db": 12}), Effect(type="gain", params={"gain_db": -12})])
    assert a.shape == b.shape and not np.allclose(a, b, atol=1e-6)


def test_stereo_input_supported():
    buf = np.stack([_buf(), _buf()], axis=1)  # (n, 2)
    out = apply_effect_chain(buf, SR, [Effect(type="reverb", params={"room_size": 0.6, "wet_level": 0.4})])
    assert out.shape == buf.shape


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        EffectChain([Effect(type="nonsense", params={})])


def test_unknown_param_dropped_or_strict():
    chain = EffectChain([Effect(type="reverb", params={"room_size": 0.5, "bogus": 1})])
    assert chain.dropped_params == ["reverb.bogus"]
    with pytest.raises(ValueError):
        EffectChain([Effect(type="reverb", params={"bogus": 1})], strict=True)


def test_params_clamped_to_range():
    chain = EffectChain([Effect(type="reverb", params={"wet_level": 2.0})])
    assert chain._plugins[0].wet_level == pytest.approx(1.0)
    out = apply_effect_chain(_buf(), SR, [Effect(type="gain", params={"gain_db": 999.0})])
    assert np.abs(out).max() <= np.abs(_buf()).max() * (10 ** (24.0 / 20.0)) + 1e-4


def test_effect_tail_seconds():
    assert effect_tail_seconds([]) == 0.0
    assert effect_tail_seconds([Effect(type="gain", params={})]) == 0.0
    assert effect_tail_seconds([Effect(type="reverb", params={"room_size": 0.8})]) > 2.0
    assert effect_tail_seconds([Effect(type="delay", params={"delay_seconds": 0.5, "feedback": 0.5})]) > 0.5


def test_validate_effect():
    assert validate_effect(Effect(type="reverb", params={"room_size": 0.5})) == []
    probs = validate_effect(Effect(type="reverb", params={"bogus": 1}))
    assert probs and "bogus" in probs[0]
    assert validate_effect(Effect(type="nope", params={}))


def test_effect_kinds_include_core_set():
    kinds = set(effect_kinds())
    assert {"reverb", "delay", "compressor", "gain", "highpass", "lowpass", "limiter"} <= kinds


# ------------------------------------------------------------------
# 混音层集成（render_buses）
# ------------------------------------------------------------------


def _session_with_fx(effect_list, note_len: float = 0.3):
    tr = Track(
        name="t",
        instrument=Instrument(program="piano", volume=1.0, effects=effect_list),
        notes=[Note(start=0.0, end=note_len, pitch_midi=60, pitch_hz=261.63, velocity=1.0)],
    )
    score = Score(title="t", tempo=100.0, tracks=[tr], buses=[], master=Bus(name="master"))
    return HostSession(score, [HostTrack(track=tr, source=_OneShotSource(note_len=note_len))], samplerate=SR)


def test_render_extends_for_reverb_tail_and_keeps_tail_energy():
    dry = render_buses(_session_with_fx([]), stereo=True)
    fx = [Effect(type="reverb", params={"room_size": 0.8, "wet_level": 0.5, "dry_level": 0.5})]
    wet = render_buses(_session_with_fx(fx), stereo=True)
    # 长度 = 时长 + 1s + reverb 尾巴
    tail = effect_tail_seconds(fx)
    expected = int(round((0.3 + 1.0 + tail) * SR))
    assert len(wet) == expected > len(dry)
    # 干声尾部静默区（note 0.3s 后）在 wet 里有余能
    seg = wet[int(0.5 * SR):]
    assert np.abs(seg).max() > 1e-3


def test_muted_track_reverb_not_leaking():
    """mute 的轨在效果链之前被剔除 → 不该漏出任何残响。"""
    s = _session_with_fx([Effect(type="reverb", params={"room_size": 0.8, "wet_level": 0.8})])
    s.tracks[0].track.mute = True
    out = render_buses(s, stereo=True)
    assert np.abs(out).max() < 1e-9
