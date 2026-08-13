"""edit.py 单元测试（M4 人在环）：人工标注应用 + 校验层拒绝 + diff_summary 格式。

LLM 调用不在此测（离线可跑精神），LLM 路径用 monkeypatch 模拟。
"""

import json

import pytest

from tsov.analysis.edit import EditResult, apply_annotations, build_diff_summary, edit_score, _validate_llm_notes
from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track


def _score():
    return Score(
        title="t", tempo=120.0,
        tracks=[Track(name="melody", instrument=Instrument(), notes=[
            Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6, velocity=0.8, confidence=0.9, deviation_cents=5.0),
            Note(start=0.6, end=1.1, pitch_midi=62, pitch_hz=293.7, velocity=0.8, confidence=0.8, deviation_cents=-8.0),
            Note(start=1.2, end=1.7, pitch_midi=64, pitch_hz=329.6, velocity=0.8, confidence=0.95, deviation_cents=0.0),
        ])],
    )


# ---- 人工标注 ----

def test_annotation_pitch():
    from tsov.dsp.pitch import midi_to_hz

    notes, err = apply_annotations(_score().tracks[0].notes, [{"index": 0, "action": "pitch", "value": 64}])
    assert err == ""
    assert notes[0].pitch_midi == 64
    assert notes[0].pitch_hz == pytest.approx(midi_to_hz(64), abs=1e-6)
    assert notes[0].deviation_cents == 0.0


def test_annotation_delete():
    notes, err = apply_annotations(_score().tracks[0].notes, [{"index": 1, "action": "delete"}])
    assert err == ""
    assert len(notes) == 2 and notes[1].pitch_midi == 64


def test_annotation_add():
    notes, err = apply_annotations(_score().tracks[0].notes, [{"index": 1, "action": "add", "value": {"pitch_midi": 65, "start": 0.55, "end": 0.6}}])
    assert err == ""
    assert len(notes) == 4 and notes[1].pitch_midi == 65


def test_annotation_merge():
    notes, err = apply_annotations(_score().tracks[0].notes, [{"index": 0, "action": "merge"}])
    assert err == ""
    assert len(notes) == 2
    assert abs(notes[0].start - 0.0) < 1e-6 and abs(notes[0].end - 1.1) < 1e-6  # 时长并集


def test_annotation_invalid_rejected():
    _, err = apply_annotations(_score().tracks[0].notes, [{"index": 99, "action": "delete"}])
    assert "越界" in err
    _, err = apply_annotations(_score().tracks[0].notes, [{"index": 0, "action": "pitch", "value": 200}])
    assert "非法" in err
    _, err = apply_annotations(_score().tracks[0].notes, [{"index": 0, "action": "bogus"}])
    assert "未知" in err


# ---- 校验层 ----

def test_validate_rejects_bad():
    with pytest.raises(ValueError):
        _validate_llm_notes([])
    with pytest.raises(ValueError):
        _validate_llm_notes([{"start": 0, "end": 0.5, "pitch_midi": 999}])  # pitch 越界
    with pytest.raises(ValueError):
        _validate_llm_notes([{"start": 0.5, "end": 0.2, "pitch_midi": 60}])  # start>=end
    with pytest.raises(ValueError):
        _validate_llm_notes([{"start": 0.0, "end": 0.5}])  # 缺 pitch_midi


def test_validate_accepts_good():
    out = _validate_llm_notes([{"start": 0.0, "end": 0.5, "pitch_midi": 64, "deviation_cents": 3.0}])
    assert len(out) == 1 and out[0].pitch_midi == 64


# ---- diff_summary 格式 ----

def test_diff_summary_format():
    orig = _score().tracks[0].notes
    new = [
        Note(start=0.0, end=0.5, pitch_midi=72, pitch_hz=523.3),  # 改音 +12st
        Note(start=0.6, end=1.1, pitch_midi=62, pitch_hz=293.7),  # 不变
    ]
    diffs = build_diff_summary(orig, new)
    assert any("→" in d and "+12st" in d for d in diffs)
    assert any("删除" in d for d in diffs)


# ---- edit_score 主流程（LLM mock）----

def test_edit_score_annotations_only(monkeypatch):
    result = edit_score(_score(), feedback="", annotations=[{"index": 0, "action": "pitch", "value": 64}], llm=False)
    assert result.error == ""
    assert result.new_score.tracks[0].notes[0].pitch_midi == 64
    assert any("→" in d for d in result.diff_summary)


def test_edit_score_llm_mocked(monkeypatch):
    def fake_llm(notes, feedback, suspicious, **params):
        raw = [{"start": n.start, "end": n.end, "pitch_midi": n.pitch_midi + 12} for n in notes]
        return _validate_llm_notes(raw), ""

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_llm)
    result = edit_score(_score(), feedback="整体高八度", llm=True)
    assert result.llm_used and result.error == ""
    assert all(n.pitch_midi == o.pitch_midi + 12 for n, o in zip(result.new_score.tracks[0].notes, _score().tracks[0].notes))
    assert any("+12st" in d for d in result.diff_summary)


def test_edit_score_llm_rejected_keeps_original(monkeypatch):
    def fake_llm(notes, feedback, suspicious, **params):
        return [], "LLM 输出非法"

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_llm)
    result = edit_score(_score(), feedback="改一下", llm=True)
    assert result.error  # 拒绝
    assert len(result.new_score.tracks[0].notes) == 3  # 保留原谱


def test_edit_result_roundtrip_json():
    score = _score()
    result = edit_score(score, annotations=[{"index": 0, "action": "pitch", "value": 64}], llm=False)
    d = json.loads(json.dumps(result.new_score.to_dict(), ensure_ascii=False))
    restored = Score.from_dict(d)
    assert restored.tracks[0].notes[0].pitch_midi == 64
