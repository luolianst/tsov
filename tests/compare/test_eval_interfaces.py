"""eval 接口守门：ADR-0006 冻结的接口签名存在且已实现（M2）。

M2 填充实现后，此处验证真实指标计算（自足 + 参考谱对比）。
"""

from tsov.core import Note, Voice
from tsov.eval import compare_to_reference, compute_self_contained


def _voice() -> Voice:
    return Voice(
        notes=[
            Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6, confidence=0.9, deviation_cents=5.0),
            Note(start=0.6, end=1.1, pitch_midi=62, pitch_hz=293.7, confidence=0.8, deviation_cents=-8.0),
            Note(start=1.2, end=1.7, pitch_midi=64, pitch_hz=329.6, confidence=0.95, deviation_cents=0.0),
        ],
        bpm=120.0,
        bpm_confidence=0.9,
        source_audio="samples/raw/哼唱/xxx.m4a",
        backend="crepe_notes",
    )


def test_compute_self_contained_returns_metrics():
    m = compute_self_contained(_voice())
    assert m.pitch_accuracy["n"] == 3
    assert "mean_cents" in m.pitch_accuracy
    assert 0.0 <= m.confidence["low_conf_ratio"] <= 1.0
    assert m.note_count["actual"] == 3


def test_compare_to_reference_matches_identical():
    voice = _voice()
    ref = [Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6),
           Note(start=0.6, end=1.1, pitch_midi=62, pitch_hz=293.7),
           Note(start=1.2, end=1.7, pitch_midi=64, pitch_hz=329.6)]
    m = compare_to_reference(voice, ref, extract_melody_first=False)
    assert m.precision == 1.0
    assert m.recall == 1.0
    assert m.f1 == 1.0


def test_compare_to_reference_no_match():
    voice = _voice()
    ref = [Note(start=0.0, end=0.5, pitch_midi=50, pitch_hz=146.8)]
    m = compare_to_reference(voice, ref, extract_melody_first=False)
    assert m.precision == 0.0
    assert m.recall == 0.0
    assert m.f1 == 0.0
