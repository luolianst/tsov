"""tsov.core 数据模型单元测试（M1 占位）：六类模型序列化 round-trip（ADR-0005 schema 冻结）。"""

from tsov.core import Effect, Instrument, KeyCandidate, Note, Score, Segment, Track, Voice


def test_note_roundtrip():
    note = Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6, velocity=0.8, confidence=0.95, deviation_cents=-3.0, is_ornament=False)
    restored = Note.from_dict(note.to_dict())
    assert restored == note


def test_voice_roundtrip_with_segments():
    voice = Voice(
        notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6)],
        bpm=120.0,
        bpm_confidence=0.7,
        segments=[Segment(start=0.0, end=0.5, type="phrase")],
        source_audio="sample.wav",
        backend="crepe_notes",
    )
    restored = Voice.from_dict(voice.to_dict())
    assert restored == voice


def test_score_roundtrip_with_tracks_and_instrument():
    score = Score(
        title="test",
        tempo=120.0,
        key_candidates=[KeyCandidate(key="C major", confidence=0.9)],
        tracks=[
            Track(
                name="melody",
                instrument=Instrument(backend="fluidsynth", program="piano", volume=0.8, effects=[Effect(type="reverb", params={"amount": 0.2})]),
                notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6)],
            )
        ],
        meta={"run_id": "m1-test"},
    )
    restored = Score.from_dict(score.to_dict())
    assert restored == score


def test_schema_fields_frozen():
    """ADR-0005 类定义字段抽查——改 schema 需先写新 ADR。"""
    assert list(Note.__dataclass_fields__) == ["start", "end", "pitch_midi", "pitch_hz", "velocity", "confidence", "deviation_cents", "is_ornament"]
