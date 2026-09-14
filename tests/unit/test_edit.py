"""edit.py 单元测试（M4 人在环）：人工标注应用 + 校验层拒绝 + diff_summary 格式。

LLM 调用不在此测（离线可跑精神），LLM 路径用 monkeypatch 模拟。
"""

import json

import pytest

from tsov.analysis.edit import (EditResult, _execute_actions, apply_annotations,
                                  build_diff_summary, edit_score, _validate_llm_notes)
from tsov.analysis.key import detect_key
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


def test_validate_allow_empty_only_for_from_scratch():
    # 空谱创作：允许空输出（保留原空谱，不再硬拒）
    assert _validate_llm_notes([], allow_empty=True) == []
    # 非空输入：仍硬拒空输出（防 LLM 清空已有谱）
    with pytest.raises(ValueError):
        _validate_llm_notes([], allow_empty=False)


def test_edit_score_from_empty_accepts_generated(monkeypatch):
    # 空谱 + LLM 创作（生成音符）→ 正常落定（legacy 整谱路径）
    def fake_llm(notes, feedback, suspicious, allow_empty=False, **params):
        raw = [{"start": 0.0, "end": 0.6, "pitch_midi": 64, "velocity": 0.8}]
        return _validate_llm_notes(raw, allow_empty), None, ""

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_llm)
    empty = Score(title="e", tempo=100.0, tracks=[Track(name="m", instrument=Instrument(), notes=[])])
    result = edit_score(empty, feedback="写一段 C 大调旋律", llm=True)
    assert result.error == ""
    assert len(result.new_score.tracks[0].notes) == 1
    assert result.new_score.tracks[0].notes[0].pitch_midi == 64


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
        return _validate_llm_notes(raw), None, ""

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_llm)
    result = edit_score(_score(), feedback="整体高八度", llm=True)
    assert result.llm_used and result.error == ""
    assert all(n.pitch_midi == o.pitch_midi + 12 for n, o in zip(result.new_score.tracks[0].notes, _score().tracks[0].notes))
    assert any("+12st" in d for d in result.diff_summary)


def test_edit_score_llm_rejected_keeps_original(monkeypatch):
    def fake_llm(notes, feedback, suspicious, **params):
        return [], None, "LLM 输出非法"

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_llm)
    result = edit_score(_score(), feedback="改一下", llm=True)
    assert result.error  # 拒绝
    assert len(result.new_score.tracks[0].notes) == 3  # 保留原谱


def test_edit_score_llm_actions_intent_diff(monkeypatch):
    """M-V2.2：LLM 返回动作数组 → 执行 + 意图级摘要（动作即 diff，不做位置对齐）。"""
    def fake_llm(notes, feedback, suspicious, **params):
        acts = [
            {"action": "pitch", "index": 0, "value": 72},
            {"action": "delete", "index": 2},
            {"action": "add", "index": 2, "value": {"pitch_midi": 65, "start": 1.2, "end": 1.7}},
        ]
        new, err = _execute_actions(notes, acts)
        assert err == ""
        return new, acts, ""

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_llm)
    result = edit_score(_score(), feedback="首音改 C5、删第三音、补一个 F4", llm=True)
    assert result.llm_used and result.error == ""
    notes = result.new_score.tracks[0].notes
    assert len(notes) == 3
    assert notes[0].pitch_midi == 72 and notes[1].pitch_midi == 62 and notes[2].pitch_midi == 65
    # 意图级摘要直接来自动作（精确，非位置对齐猜）
    assert any("idx0" in d and "+12st" in d for d in result.diff_summary)
    assert any("idx2" in d and "删除" in d for d in result.diff_summary)
    assert any("idx2" in d and "add" in d for d in result.diff_summary)
    assert result.actions and len(result.actions) == 3
    assert result.actions[0]["action"] == "pitch" and result.actions[1]["action"] == "delete"


def test_edit_result_roundtrip_json():
    score = _score()
    result = edit_score(score, annotations=[{"index": 0, "action": "pitch", "value": 64}], llm=False)
    d = json.loads(json.dumps(result.new_score.to_dict(), ensure_ascii=False))
    restored = Score.from_dict(d)
    assert restored.tracks[0].notes[0].pitch_midi == 64


# ---- M-V2.3 审计回归（2026-09-14）----

def test_edit_score_from_scratch_no_tracks(monkeypatch):
    """web 新建工程落盘 tracks=[] → edit_score 自动补空轨、从零创作（原先硬拒=空谱创作不可用）。"""
    def fake_llm(notes, feedback, suspicious, allow_empty=False, **params):
        acts = [{"action": "add", "index": 0, "value": {"pitch_midi": 60, "start": 0.0, "end": 0.5}}]
        new, err = _execute_actions(notes, acts)
        assert err == ""
        return new, acts, ""

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_llm)
    empty = Score(title="scratch", tempo=120.0, tracks=[])   # 与 web POST /api/projects 一致
    result = edit_score(empty, feedback="写一小段 C 大调旋律，4 个音", llm=True)
    assert result.error == ""
    assert len(result.new_score.tracks) == 1
    assert [n.pitch_midi for n in result.new_score.tracks[0].notes] == [60]


def test_edit_score_transpose_syncs_key(monkeypatch):
    """审计回归：动作路径 transpose 后 key_candidates 必须重算（别名比较曾让此判断恒等跳过）。"""
    from tsov.dsp.pitch import midi_to_hz

    def fake_llm(notes, feedback, suspicious, allow_empty=False, **params):
        acts = [{"action": "transpose", "value": 2}]
        new, err = _execute_actions(notes, acts)
        assert err == ""
        return new, acts, ""

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_llm)
    base = Score(title="k", tempo=100.0, tracks=[Track(name="m", instrument=Instrument(), notes=[
        Note(start=i * 0.5, end=i * 0.5 + 0.4, pitch_midi=p, pitch_hz=midi_to_hz(p))
        for i, p in enumerate([60, 62, 64, 65, 67, 69])])])
    result = edit_score(base, feedback="整体 +2 半音", llm=True)
    assert result.error == ""
    notes = result.new_score.tracks[0].notes
    assert [n.pitch_midi for n in notes] == [62, 64, 66, 67, 69, 71]
    assert result.new_score.key_candidates, "key_candidates 为空 = 重算未执行（别名恒等回归）"
    assert [k.key for k in result.new_score.key_candidates] == [k.key for k in detect_key(notes)]


# ---- M-V2.4 防误清空守卫（B2#2 手册实测发现的漏洞）----

def test_edit_score_rejects_clear_all_actions(monkeypatch):
    """B2#2：非空输入 + delete-all 动作 → 拒绝、原谱不变（原先动作路径绕过 legacy 空输出硬拒）。

    注：delete 按输入序列 index 引用、会移动后续索引 → 须从后往前删（与 LLM 提示词约定一致）。
    """
    def fake_llm(notes, feedback, suspicious, allow_empty=False, **params):
        acts = [{"action": "delete", "index": i} for i in range(len(notes) - 1, -1, -1)]
        new, err = _execute_actions(notes, acts)
        assert err == ""
        return new, acts, ""

    monkeypatch.setattr("tsov.analysis.edit._call_edit_llm", fake_llm)
    result = edit_score(_score(), feedback="清空所有音符", llm=True)
    assert result.error and "空" in result.error, f"应拒绝：{result.error!r}"
    assert [n.pitch_midi for n in result.new_score.tracks[0].notes] == [60, 62, 64]  # 原谱保留


def test_edit_score_rejects_clear_all_annotations():
    """同一守卫覆盖人工标注路径：delete-all 标注 → 拒绝、原谱不变。"""
    anns = [{"index": i, "action": "delete"} for i in range(2, -1, -1)]
    result = edit_score(_score(), annotations=anns, llm=False)
    assert result.error and "空" in result.error, f"应拒绝：{result.error!r}"
    assert len(result.new_score.tracks[0].notes) == 3
