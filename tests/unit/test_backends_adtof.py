"""批E 段2 单测：ADTOF 鼓转录后端（E2-1/E2-3）。

- 注册表：adtof 已注册
- 映射：峰值 → GM 打击乐 Note（同刻叠音保留 / 排序 / 字段齐）
- 资产：vendor 缺失指路；假 vendor 结构就位判定
"""

from __future__ import annotations

import pytest

from tsov.core.units import midi_to_hz
from tsov.dsp.backends import get_backend, list_backends


def test_adtof_registered():
    assert "adtof" in list_backends()
    assert get_backend("adtof").name == "adtof"


def test_peaks_to_notes_mapping_and_stacking():
    from tsov.dsp.backends.adtof import peaks_to_notes

    peaks = {35: [0.5, 1.0], 38: [0.5], 42: [0.5, 0.25], 49: [2.0]}
    notes = peaks_to_notes(peaks, velocity=0.8, note_duration=0.1)
    assert len(notes) == 6
    starts = [n.start for n in notes]
    assert starts == sorted(starts)
    # 同刻叠击合法（鼓的语义）：t=0.5 三件 35/38/42 全保留
    same = [n for n in notes if abs(n.start - 0.5) < 1e-9]
    assert sorted(n.pitch_midi for n in same) == [35, 38, 42]
    n0 = notes[0]
    assert abs((n0.end - n0.start) - 0.1) < 1e-9
    assert n0.pitch_hz == midi_to_hz(n0.pitch_midi)
    assert n0.velocity == 0.8 and n0.confidence == 1.0 and n0.is_ornament is False


def test_asset_status_missing(monkeypatch, tmp_path):
    import tsov.dsp.backends.adtof as a

    monkeypatch.setattr(a, "_VENDOR_SRC", tmp_path / "nope")
    ok, why = a.asset_status()
    assert not ok and "fetch_engines" in why


def test_asset_status_ok_fake_vendor(monkeypatch, tmp_path):
    import tsov.dsp.backends.adtof as a

    fake = tmp_path / "src"
    (fake / "adtof_pytorch" / "data").mkdir(parents=True)
    (fake / "adtof_pytorch" / "data" / "adtof_frame_rnn_pytorch_weights.pth").write_bytes(b"x")
    monkeypatch.setattr(a, "_VENDOR_SRC", fake)
    ok, why = a.asset_status()
    assert ok and why == ""


def test_transcribe_missing_vendor_raises(monkeypatch, tmp_path):
    import tsov.dsp.backends.adtof as a

    monkeypatch.setattr(a, "_VENDOR_SRC", tmp_path / "nope")
    with pytest.raises(RuntimeError, match="ADTOF 未安装"):
        get_backend("adtof").transcribe(str(tmp_path / "x.wav"))
