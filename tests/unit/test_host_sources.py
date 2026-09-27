"""音源适配器测试（ADR-0013 里程碑二）：VST3Source（pedalboard）/ SFZSource（内置最小采样器）。

约定：不用 pytest tmp_path（handoff 坑 81）——手动 output/<uuid> 目录 + teardown rmtree。
VST3 用例依赖 vendor/vst3/Dexed.vst3（不入库；缺失自动 skip）。
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

from tsov.core.notes import Note
from tsov.host.instrument import SF2Source, SFZSource, VST3Source, _parse_sfz, make_source

from unit._cleanup import rmtree_force

_DEXED = Path("vendor/vst3/Dexed.vst3/Contents/x86_64-win/Dexed.vst3")
try:
    import pedalboard  # noqa: F401

    _HAS_PEDALBOARD = True
except ImportError:  # pragma: no cover
    _HAS_PEDALBOARD = False


def _note(start, end, pitch, vel=0.8):
    return Note(start=start, end=end, pitch_midi=pitch, pitch_hz=440.0 * 2 ** ((pitch - 69) / 12), velocity=vel)


def _dominant_hz(seg: np.ndarray, sr: int = 44100) -> float:
    seg = seg - float(seg.mean())
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / sr)
    return float(freqs[int(np.argmax(spec))])


def _centroid_hz(seg: np.ndarray, sr: int = 44100) -> float:
    seg = seg - float(seg.mean())
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / sr)
    return float((spec * freqs).sum() / max(spec.sum(), 1e-12))


# ---------------------------------------------------------------------------
# SFZ（内置最小采样器）
# ---------------------------------------------------------------------------


def test_sfz_source_renders_and_pitches():
    """合成 A4 正弦样本 + 最小 SFZ → 渲染两个音：非静音 + 主频≈音高（变调正确）。"""
    import soundfile as sf

    d = Path("output") / f"sfztest-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        sr = 44100
        t = np.arange(int(sr * 1.0)) / sr
        tone = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        sf.write(str(d / "a4.wav"), tone, sr, subtype="PCM_16")
        (d / "test.sfz").write_text(
            "<global>\n<region> sample=a4.wav lokey=0 hikey=127 pitch_keycenter=69\n", encoding="utf-8"
        )
        src = SFZSource(str(d / "test.sfz"))
        notes = [_note(0.0, 0.5, 69), _note(0.5, 1.0, 71)]
        out = src.render(notes, sr, int(sr * 1.0))
        assert out.shape == (sr,)
        assert float(np.abs(out).max()) > 0.05, "应有声音"
        f1 = _dominant_hz(out[: int(sr * 0.45)])
        f2 = _dominant_hz(out[int(sr * 0.5): int(sr * 0.95)])
        assert abs(f1 - 440.0) / 440.0 < 0.03, f"段1 主频 {f1}"
        assert abs(f2 - 493.88) / 493.88 < 0.03, f"段2 主频 {f2}"
    finally:
        rmtree_force(d)   # 清理：.git/objects 只读属性 → chmod 强删（见 _cleanup.py）


def test_sfz_parse_subset_and_key_alias():
    """子集解析：global/group 继承、key= 别名、注释剥离。"""
    d = Path("output") / f"sfztest-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        (d / "x.sfz").write_text(
            "// 注释行\n"
            "<global>\nvolume=-3\n"
            "<group>\nlovel=1 hivel=127\n"
            "<region> sample=a.wav key=60  // 行尾注释\n"
            "<region> sample=b.wav lokey=100 hikey=110\n",
            encoding="utf-8",
        )
        regions = _parse_sfz(d / "x.sfz")
        assert len(regions) == 2
        assert regions[0]["sample"] == "a.wav" and regions[0]["key"] == 60
        assert regions[0]["volume"] == -3          # 继承 global
        assert regions[0]["hivel"] == 127          # 继承 group
        assert regions[1]["lokey"] == 100 and regions[1]["sample"] == "b.wav"
    finally:
        rmtree_force(d)   # 清理：.git/objects 只读属性 → chmod 强删（见 _cleanup.py）


# ---------------------------------------------------------------------------
# SF2（FluidSynth）——关键回归：打击乐仓库位
# ---------------------------------------------------------------------------


def _sf2_available() -> bool:
    try:
        from tsov.render.fluidsynth_backend import _load_fluidsynth, default_soundfont

        _load_fluidsynth()
        return bool(default_soundfont())
    except Exception:
        return False


@pytest.mark.skipif(not _sf2_available(), reason="FluidSynth DLL 或 SoundFont 缺失")
def test_sf2_drums_use_percussion_bank():
    """回归（2026-09-27）：「内置 drum 实为钢琴」——drums 必须选 GM bank128 鼓组。

    bug 形态：曾对鼓轨 program_select(ch9, bank=0, preset=0) → bank0/preset0=大钢琴，
    drums 与 piano 渲染逐样本相关 ≈1.0。修复后 hihat（note 42）质心 ≈10.5kHz。
    再犯即红：质心暴跌（≈1.2kHz）且与钢琴高度相关。
    """
    from tsov.render.fluidsynth_backend import default_soundfont

    sr = 44100
    n = int(0.9 * sr)

    src_d = SF2Source(default_soundfont(), program="drums")
    try:
        out_d = np.asarray(src_d.render([_note(0.0, 0.5, 42)], sr, n), dtype=np.float64)
    finally:
        src_d.close()
    src_p = SF2Source(default_soundfont(), program="piano")
    try:
        out_p = np.asarray(src_p.render([_note(0.0, 0.5, 42)], sr, n), dtype=np.float64)
    finally:
        src_p.close()

    cd = _centroid_hz(out_d)
    corr = float(np.corrcoef(out_d, out_p)[0, 1])
    assert cd > 5000.0, f"鼓质心 {cd:.0f}Hz 不像打击乐（hihat 应 ≈10kHz，钢琴 ≈1.2kHz）"
    assert abs(corr) < 0.5, f"drums 与 piano 相关 {corr:.2f}——鼓音源疑似又变回钢琴"


# ---------------------------------------------------------------------------
# VST3（pedalboard 底座）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_PEDALBOARD or not _DEXED.is_file(), reason="pedalboard 或 vendor/vst3/Dexed 缺失")
def test_vst3_source_renders_instrument():
    """真 VST3 乐器（Dexed）：加载 → MIDI 渲染 → 非静音；音符前接近静音。"""
    src = VST3Source(str(_DEXED))
    sr = 44100
    notes = [_note(0.1, 0.6, 60), _note(0.2, 0.7, 64)]
    out = src.render(notes, sr, int(sr * 0.9))
    assert out.shape == (int(sr * 0.9),)
    peak = float(np.abs(out).max())
    assert peak > 0.005, "VST3 应出声"
    pre = float(np.abs(out[: int(sr * 0.05)]).max())
    assert pre < peak, "第一个音符前应比峰值安静"


def test_make_source_dispatch():
    """工厂分发：vst3:/sfz: 前缀 → 对应音源；其余 → SF2（缺 soundfont 报错）。"""
    if _HAS_PEDALBOARD and _DEXED.is_file():
        src = make_source(f"vst3:{_DEXED}")
        assert isinstance(src, VST3Source)
        src.close()
    with pytest.raises(RuntimeError):
        make_source("piano")  # 无 soundfont
    with pytest.raises(RuntimeError):
        make_source("sfz:no-such-file.sfz")
