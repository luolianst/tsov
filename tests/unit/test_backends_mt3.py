"""批E 段2 单测：MT3 多器乐后端（E2-2/E2-3）。

- 注册表：mt3 已注册
- 转换：mido MIDI → Note（复音保留 / 碎音剔除 / keep_programs 过滤 / velocity 换算）
- 资产：权重缺失指路；无 auto_download 时报错
"""

from __future__ import annotations

import io

import pytest

from tsov.core.units import midi_to_hz
from tsov.dsp.backends import get_backend, list_backends


def _build_mido():
    """构造测试 MIDI：贝斯 3 音（含 20ms 碎音）+ 吉他 2 音（同刻叠音，velocity 127）。"""
    import mido
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(initial_tempo=130.0)
    bass = pretty_midi.Instrument(program=33, name="bass")
    bass.notes.append(pretty_midi.Note(velocity=100, pitch=40, start=0.0, end=0.5))
    bass.notes.append(pretty_midi.Note(velocity=80, pitch=43, start=0.5, end=0.9))
    bass.notes.append(pretty_midi.Note(velocity=90, pitch=40, start=1.0, end=1.02))  # 20ms 碎音
    gtr = pretty_midi.Instrument(program=24, name="guitar")
    gtr.notes.append(pretty_midi.Note(velocity=127, pitch=64, start=0.0, end=0.5))
    gtr.notes.append(pretty_midi.Note(velocity=60, pitch=67, start=0.0, end=0.4))  # 同刻叠音
    pm.instruments.extend([bass, gtr])
    buf = io.BytesIO()
    pm.write(buf)
    buf.seek(0)
    return mido.MidiFile(file=buf)


def test_mt3_registered():
    assert "mt3" in list_backends()
    assert get_backend("mt3").name == "mt3"


def test_midi_to_notes_rules():
    from tsov.dsp.backends.mt3 import midi_to_notes

    midi = _build_mido()
    notes = midi_to_notes(midi, min_note_ms=40.0)
    assert len(notes) == 4  # 20ms 碎音剔除
    starts = [n.start for n in notes]
    assert starts == sorted(starts)
    same = [n for n in notes if abs(n.start - 0.0) < 1e-9]
    assert sorted(n.pitch_midi for n in same) == [40, 64, 67]  # 同刻叠音保留
    g = [n for n in notes if n.pitch_midi == 64][0]
    assert abs(g.velocity - 1.0) < 1e-9  # 127 → 1.0
    only_bass = midi_to_notes(midi, min_note_ms=40.0, keep_programs=[33])
    assert {n.pitch_midi for n in only_bass} == {40, 43}
    assert notes[0].pitch_hz == midi_to_hz(notes[0].pitch_midi)


def test_asset_status_checkpoint_missing(monkeypatch, tmp_path):
    import tsov.dsp.backends.mt3 as m

    monkeypatch.setattr(m, "_CHECKPOINT_DIR", tmp_path / "nope")
    ok, why = m.asset_status()
    assert not ok and "权重缺失" in why


def test_transcribe_no_autodownload_raises(monkeypatch, tmp_path):
    import tsov.dsp.backends.mt3 as m

    monkeypatch.setattr(m, "_CHECKPOINT_DIR", tmp_path / "nope")
    with pytest.raises(RuntimeError, match="权重缺失"):
        get_backend("mt3").transcribe(str(tmp_path / "x.wav"), auto_download=False)
