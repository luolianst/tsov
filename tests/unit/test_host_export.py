"""M-V4 导出矩阵测试：文件齐全 / 统一缩放 / stereo-mono 选项 / stems 与 mix 关系。"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tsov.core.notes import Note
from tsov.core.score import Bus, Instrument, Score, Track
from tsov.host import HostEngine, export_matrix

_HAS_SF = Path("vendor/soundfonts/FluidR3_GM.sf2").is_file()

pytestmark = pytest.mark.skipif(not _HAS_SF, reason="缺 vendor/soundfonts/FluidR3_GM.sf2")


def _score():
    notes_a = [Note(start=i * 0.3, end=i * 0.3 + 0.25, pitch_midi=p, pitch_hz=440.0, velocity=0.8)
               for i, p in enumerate([60, 62, 64, 65])]
    notes_b = [Note(start=i * 0.4, end=i * 0.4 + 0.35, pitch_midi=p, pitch_hz=110.0, velocity=0.7)
               for i, p in enumerate([36, 36, 43])]
    return Score(
        title="exp", tempo=110.0,
        tracks=[
            Track(name="melody", instrument=Instrument(program="piano"), notes=notes_a),
            Track(name="bass", instrument=Instrument(program="bass"), notes=notes_b, bus="b1"),
        ],
        buses=[Bus(name="b1", volume=0.5)],
    )


def test_export_matrix_full():
    out = Path("output") / f"exptest-{uuid.uuid4().hex[:8]}"
    try:
        engine = HostEngine()
        session = engine.load(_score())
        try:
            report = export_matrix(session, out, midi_stems=True)
        finally:
            session.close()

        # 文件齐全
        assert Path(report["mix"]).is_file()
        assert (out / "buses" / "b1.wav").is_file()
        assert len(report["stems"]) == 2 and all(Path(p).is_file() for p in report["stems"].values())
        assert Path(report["midi"]).is_file()
        assert len(report["midi_stems"]) == 2

        # stereo 默认 + 时长 ≈ duration+1（最后音 end=1.15s）
        mix, sr = sf.read(report["mix"], dtype="float32")
        assert mix.ndim == 2 and mix.shape[1] == 2
        assert abs(mix.shape[0] / sr - 2.15) < 0.05

        # stems 与 mix 关系：mix ≈ (stem_melody + 0.5*stem_bass) * scale（总线/mute 无、master=1）
        stem_m, _ = sf.read(report["stems"]["melody"], dtype="float32")
        stem_b, _ = sf.read(report["stems"]["bass"], dtype="float32")
        expect = (stem_m + 0.5 * stem_b) * report["scale"]
        assert np.allclose(mix[: len(expect)], expect, atol=2e-3)

        # MIDI 多轨
        import pretty_midi

        midi = pretty_midi.PrettyMIDI(report["midi"])
        assert len(midi.instruments) == 2
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_export_mono_and_minimal():
    out = Path("output") / f"exptest-{uuid.uuid4().hex[:8]}"
    try:
        engine = HostEngine()
        session = engine.load(_score())
        try:
            report = export_matrix(session, out, stereo=False, stems=False, buses=False, midi=False)
        finally:
            session.close()
        mix, _ = sf.read(report["mix"], dtype="float32")
        assert mix.ndim == 1
        assert report["files"] == [report["mix"]]   # 只有 mix
    finally:
        shutil.rmtree(out, ignore_errors=True)


# ---------------- M-V8 E6 段2：位深 / 选段导出 ----------------

def test_write_wav_subtype_param():
    """E6 段2：write_wav subtype 参数（PCM_16 / PCM_24 / FLOAT＝32f）。"""
    from tsov.host.device import write_wav

    out = Path("output") / f"exptest-{uuid.uuid4().hex[:8]}"
    try:
        audio = (np.sin(np.linspace(0, 40, 8000)) * 0.3).astype(np.float64)
        for sub, bits in [("PCM_16", 16), ("PCM_24", 24), ("FLOAT", 32)]:
            p = write_wav(audio, out / f"{sub}.wav", 8000, subtype=sub)
            assert sf.info(p).subtype == sub
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_export_matrix_bit_depth_and_range():
    """E6 段2：位深 24/32f 实测文件头；选段 = 全曲同区间样本级切片；MIDI 不受选段影响。"""
    out_full = Path("output") / f"exptest-{uuid.uuid4().hex[:8]}-full"
    out_cut = Path("output") / f"exptest-{uuid.uuid4().hex[:8]}-cut"
    try:
        engine = HostEngine()
        session = engine.load(_score())
        try:
            rep_full = export_matrix(session, out_full, stems=False, buses=False, midi=True, bit_depth="32f")
            rep_cut = export_matrix(session, out_cut, stems=False, buses=False, midi=True,
                                    bit_depth=32, range=[0.3, 1.0])
            rep24 = export_matrix(session, out_cut / "b24", stems=False, buses=False, midi=False, bit_depth=24)
        finally:
            session.close()

        assert rep_full["bit_depth"] == "FLOAT" and rep_cut["bit_depth"] == "FLOAT"
        assert rep_cut["range"] == [0.3, 1.0]
        assert sf.info(rep_full["mix"]).subtype == "FLOAT"
        assert sf.info(rep24["mix"]).subtype == "PCM_24"

        sr = rep_full["samplerate"]
        full, _ = sf.read(rep_full["mix"], dtype="float32")
        cut, _ = sf.read(rep_cut["mix"], dtype="float32")
        s0, s1 = round(0.3 * sr), round(1.0 * sr)
        assert cut.shape[0] == s1 - s0, (cut.shape, s1 - s0)
        # 同一谱连渲两次非比特一致（合成器内部状态 ~7.7e-4）→ 容差比对；平移对照位证明样本对齐
        err = float(np.max(np.abs(cut - full[s0:s1])))
        shift1 = float(np.max(np.abs(cut[:-1] - full[s0 + 1:s1])))
        assert err < 5e-3, err                      # 同区间（对齐）
        assert shift1 > 10 * err, (err, shift1)     # 1 样本平移即现形（非平移误对齐）

        # MIDI 维持全曲（不被选段裁剪）：cut 版 midi 尾 ≈ 1.15s（全曲）；若被裁则 ≈ 0.7s
        import pretty_midi

        midi = pretty_midi.PrettyMIDI(rep_cut["midi"])
        assert len(midi.instruments) == 2 and midi.get_end_time() > 1.1
    finally:
        shutil.rmtree(out_full, ignore_errors=True)
        shutil.rmtree(out_cut, ignore_errors=True)


def test_export_matrix_invalid_opts():
    """E6 段2：非法位深 / 非法选段 → ValueError（服务端转 400）。"""
    out = Path("output") / f"exptest-{uuid.uuid4().hex[:8]}"
    engine = HostEngine()
    session = engine.load(_score())
    try:
        for bad in ("12", 8, "abc"):
            with pytest.raises(ValueError):
                export_matrix(session, out, stems=False, buses=False, midi=False, bit_depth=bad)
        with pytest.raises(ValueError):
            export_matrix(session, out, stems=False, buses=False, midi=False, range=[1.0, 1.0])
        with pytest.raises(ValueError):
            export_matrix(session, out, stems=False, buses=False, midi=False, range=[-0.5, 1.0])
        with pytest.raises(ValueError):
            export_matrix(session, out, stems=False, buses=False, midi=False, range=[50.0, 60.0])
        with pytest.raises(ValueError):
            export_matrix(session, out, stems=False, buses=False, midi=False, range=[0.1])
    finally:
        session.close()
        shutil.rmtree(out, ignore_errors=True)
