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
