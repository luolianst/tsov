"""v0.2 批C 后段（P40）单测：标注按「挂起轨」分组应用。

覆盖：
- 多轨混挂：各轨只改自己的挂起轨（track[0] 不再被误改）
- 向后兼容：无 track 键的旧标注 → 归 0 轨
- 非法标注 → 整体拒绝（error 非空）
"""

from __future__ import annotations

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track


def _mk_score() -> Score:
    return Score(title="t", tempo=120.0, tracks=[
        Track(name="A", instrument=Instrument(),
              notes=[Note(start=0.0, end=1.0, pitch_midi=60, pitch_hz=261.6, velocity=0.8)]),
        Track(name="B", instrument=Instrument(),
              notes=[Note(start=0.0, end=1.0, pitch_midi=62, pitch_hz=293.7, velocity=0.8)]),
    ])


def test_annotation_groups_multi_track():
    from tsov.webapp.agent_session import _apply_annotation_groups

    s = _mk_score()
    new_score, summaries, err = _apply_annotation_groups(s, [
        {"index": 0, "track": 1, "action": "pitch", "value": 64},
        {"index": 0, "track": 0, "action": "velocity", "value": 0.5},
    ])
    assert not err and summaries
    assert new_score.tracks[1].notes[0].pitch_midi == 64        # 挂 1 轨 → 改 1 轨
    assert new_score.tracks[0].notes[0].pitch_midi == 60        # 0 轨音高不动
    assert abs(new_score.tracks[0].notes[0].velocity - 0.5) < 1e-9
    # 原 Score 不被就地修改
    assert s.tracks[1].notes[0].pitch_midi == 62


def test_annotation_groups_back_compat_track0():
    """旧格式（无 track 键）→ track[0]（历史行为不变）。"""
    from tsov.webapp.agent_session import _apply_annotation_groups

    s = _mk_score()
    new_score, summaries, err = _apply_annotation_groups(s, [{"index": 0, "action": "pitch", "value": 61}])
    assert not err
    assert new_score.tracks[0].notes[0].pitch_midi == 61
    assert new_score.tracks[1].notes[0].pitch_midi == 62


def test_annotation_groups_reject_invalid():
    from tsov.webapp.agent_session import _apply_annotation_groups

    s = _mk_score()
    _new, _summaries, err = _apply_annotation_groups(s, [{"index": 99, "track": 0, "action": "pitch", "value": 61}])
    assert err
