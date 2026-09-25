"""M-V8 E5 编辑工具集·命令层单测：split_note / merge_notes / shift_notes + quantize_time 扩展（swing/indices）。

语义：
- split_note：at 处切分，两侧各留 ≥1ms；尾段继承全部属性（音高/力度/置信度/装饰标记）。
- merge_notes：与后邻同音高合并（gap ≤ max_gap，默认 0.5s；重叠按并集）；无候选拒绝。
- shift_notes：批量时间平移，原子（任一出界整批拒绝）；indices 缺省=整轨。
- quantize_time 扩展：{grid, swing?, indices?}——swing 只作用于奇格位 start（顺延 swing×半格），
  裸 grid 数与旧行为逐位一致（向后兼容）。
"""

from __future__ import annotations

import pytest

from tsov.core.notes import Note
from tsov.core.score import Score, Track
from tsov.host import EditBatch


def _note(start=0.0, end=0.5, pm=60, vel=0.8, conf=0.9):
    return Note(start=start, end=end, pitch_midi=pm, pitch_hz=261.6, velocity=vel, confidence=conf)


def _score(notes=None, tempo=120.0):
    return Score(title="t", tempo=tempo, tracks=[Track(name="轨0", notes=notes or [_note()])])


def _apply(score, *cmds):
    b = EditBatch()
    for c in cmds:
        b.add(**c)
    return b.apply(score)


# ---------------- split_note（剪刀）----------------


def test_split_note_normal_and_inherit():
    out, res = _apply(_score([_note(0.0, 1.0, pm=64, vel=0.7, conf=0.55)]),
                      {"op": "split_note", "track": 0, "index": 0, "value": {"at": 0.4}})
    assert res.ok and res.applied == 1
    notes = out.tracks[0].notes
    assert len(notes) == 2
    a, b = notes
    assert (a.start, a.end) == (0.0, 0.4)
    assert (b.start, b.end) == (0.4, 1.0)
    assert (b.pitch_midi, b.velocity, b.confidence) == (64, 0.7, 0.55)  # 尾段继承
    assert b.pitch_hz == a.pitch_hz


def test_split_note_bare_number_value():
    out, res = _apply(_score([_note(0.0, 1.0)]),
                      {"op": "split_note", "track": 0, "index": 0, "value": 0.25})
    assert res.ok and [(n.start, n.end) for n in out.tracks[0].notes] == [(0.0, 0.25), (0.25, 1.0)]


def test_split_note_rejects():
    s = _score([_note(0.0, 1.0)])
    cases = [
        {"value": {"at": 0.0}},        # 贴边（留 <1ms）
        {"value": {"at": 1.0}},
        {"value": {"at": 0.0005}},     # 距 start 0.5ms < 1ms
        {"value": {"at": -0.5}},       # 越界
        {"value": {"at": 1.5}},
        {"value": {"at": "x"}},        # 非法
        {"value": None},
        {"index": 3, "value": {"at": 0.5}},   # index 越界
    ]
    for c in cases:
        _out, res = _apply(s, {"op": "split_note", "track": 0, **c})
        assert res.applied == 0 and res.errors, c


def test_split_merge_roundtrip():
    """切一刀再胶回去 = 原音符。"""
    s = _score([_note(0.0, 1.0, pm=60)])
    out, res = _apply(s,
                      {"op": "split_note", "track": 0, "index": 0, "value": {"at": 0.5}},
                      {"op": "merge_notes", "track": 0, "index": 0})
    assert res.ok and res.applied == 2
    notes = out.tracks[0].notes
    assert len(notes) == 1 and (notes[0].start, notes[0].end) == (0.0, 1.0)


# ---------------- merge_notes（胶水）----------------


def test_merge_notes_default_gap():
    out, res = _apply(_score([_note(0.0, 0.5), _note(0.6, 1.0)]),
                      {"op": "merge_notes", "track": 0, "index": 0})
    assert res.ok
    notes = out.tracks[0].notes
    assert len(notes) == 1 and (notes[0].start, notes[0].end) == (0.0, 1.0)


def test_merge_notes_custom_gap_and_too_far():
    s = _score([_note(0.0, 0.5), _note(1.2, 1.5)])         # gap = 0.7
    _out, res = _apply(s, {"op": "merge_notes", "track": 0, "index": 0})
    assert res.applied == 0 and "无相邻" in res.errors[0]
    out, res2 = _apply(s, {"op": "merge_notes", "track": 0, "index": 0, "value": {"max_gap": 0.8}})
    assert res2.ok and len(out.tracks[0].notes) == 1


def test_merge_notes_different_pitch_rejected():
    _out, res = _apply(_score([_note(0.0, 0.5, pm=60), _note(0.6, 1.0, pm=62)]),
                       {"op": "merge_notes", "track": 0, "index": 0})
    assert res.applied == 0 and "无相邻" in res.errors[0]


def test_merge_notes_overlap_union():
    out, res = _apply(_score([_note(0.0, 0.5), _note(0.4, 0.9)]),
                      {"op": "merge_notes", "track": 0, "index": 0})
    assert res.ok
    notes = out.tracks[0].notes
    assert len(notes) == 1 and (notes[0].start, notes[0].end) == (0.0, 0.9)


def test_merge_notes_picks_nearest_following():
    out, res = _apply(_score([_note(0.0, 0.5), _note(0.55, 0.7), _note(0.65, 0.8)]),
                      {"op": "merge_notes", "track": 0, "index": 0})
    assert res.ok
    notes = out.tracks[0].notes
    assert len(notes) == 2
    assert (notes[0].start, notes[0].end) == (0.0, 0.7)    # 并的是最近的那颗（0.55 起）
    assert notes[1].start == 0.65


def test_merge_notes_rejects():
    s = _score([_note(0.0, 0.5)])
    for c in ({"index": 5, "value": None},
              {"index": 0, "value": {"max_gap": -1}},
              {"index": 0, "value": {"max_gap": "x"}}):
        _out, res = _apply(s, {"op": "merge_notes", "track": 0, **c})
        assert res.applied == 0 and res.errors, c


# ---------------- shift_notes（微推）----------------


def test_shift_notes_selection_only():
    out, res = _apply(_score([_note(0.0, 0.5), _note(1.0, 1.5)]),
                      {"op": "shift_notes", "track": 0, "value": {"dtime": 0.25, "indices": [1]}})
    assert res.ok
    notes = out.tracks[0].notes
    assert (notes[0].start, notes[0].end) == (0.0, 0.5)     # 未选中不动
    assert (notes[1].start, notes[1].end) == (1.25, 1.75)


def test_shift_notes_whole_track_when_indices_omitted():
    out, res = _apply(_score([_note(0.0, 0.5), _note(1.0, 1.5)]),
                      {"op": "shift_notes", "track": 0, "value": {"dtime": 0.5}})
    assert res.ok
    assert [(n.start, n.end) for n in out.tracks[0].notes] == [(0.5, 1.0), (1.5, 2.0)]


def test_shift_notes_atomic_reject():
    """任一出界 → 整批拒绝（不做部分应用）。"""
    out, res = _apply(_score([_note(0.0, 0.5), _note(1.0, 1.5)]),
                      {"op": "shift_notes", "track": 0, "value": {"dtime": -0.2, "indices": [0, 1]}})
    assert res.applied == 0 and "越界" in res.errors[0]
    assert [(n.start, n.end) for n in out.tracks[0].notes] == [(0.0, 0.5), (1.0, 1.5)]


def test_shift_notes_rejects_bad_args():
    s = _score([_note(0.0, 0.5)])
    cases = [
        {"dtime": "x"}, {"dtime": None}, {},
        {"dtime": 0.1, "indices": []},
        {"dtime": 0.1, "indices": [9]},
        {"dtime": float("nan")}, {"dtime": float("inf")},
    ]
    for c in cases:
        _out, res = _apply(s, {"op": "shift_notes", "track": 0, "value": c})
        assert res.applied == 0 and res.errors, c


def test_shift_notes_dedupe_and_noop():
    out, res = _apply(_score([_note(0.0, 0.5), _note(1.0, 1.5)]),
                      {"op": "shift_notes", "track": 0, "value": {"dtime": 0.1, "indices": [1, 1]}})
    assert res.ok and out.tracks[0].notes[1].start == pytest.approx(1.1)
    _out2, res2 = _apply(_score(), {"op": "shift_notes", "track": 0, "value": {"dtime": 0.0}})
    assert res2.ok


# ---------------- quantize_time 扩展 ----------------

def test_quantize_time_backward_compat_bare_grid():
    """裸 grid 数 = 旧行为逐位一致（cell = beat/grid）。"""
    out, res = _apply(_score([_note(0.31, 0.52)]),
                      {"op": "quantize_time", "track": 0, "value": 4})
    assert res.ok
    n = out.tracks[0].notes[0]
    assert n.start == 0.25 and n.end == 0.5          # cell=0.125


def test_quantize_time_dict_no_swing_same_as_bare():
    a, _ = _apply(_score([_note(0.31, 0.52)]), {"op": "quantize_time", "track": 0, "value": 4})
    b, _ = _apply(_score([_note(0.31, 0.52)]), {"op": "quantize_time", "track": 0, "value": {"grid": 4}})
    assert [(n.start, n.end) for n in a.tracks[0].notes] == [(n.start, n.end) for n in b.tracks[0].notes]


def test_quantize_time_swing_vector():
    """测试向量：BPM=120、grid=2（1/8，cell=0.25s）、swing=0.6 → 0.25 处起音 → 0.325。"""
    s = _score([_note(0.24, 0.6), _note(0.49, 0.74)], tempo=120.0)
    out, res = _apply(s, {"op": "quantize_time", "track": 0, "value": {"grid": 2, "swing": 0.6}})
    assert res.ok
    n0, n1 = out.tracks[0].notes
    assert n0.start == pytest.approx(0.325, abs=1e-6)   # 奇格位（k=1）→ 0.25 + 0.6×0.125
    assert n0.end == pytest.approx(0.5, abs=1e-6)
    assert n1.start == pytest.approx(0.5, abs=1e-6)     # 偶格位（k=2）不动
    assert n1.end == pytest.approx(0.75, abs=1e-6)


def test_quantize_time_swing_zero_equals_off():
    a, _ = _apply(_score([_note(0.24, 0.6)]), {"op": "quantize_time", "track": 0, "value": {"grid": 2}})
    b, _ = _apply(_score([_note(0.24, 0.6)]), {"op": "quantize_time", "track": 0, "value": {"grid": 2, "swing": 0.0}})
    assert a.tracks[0].notes[0].start == b.tracks[0].notes[0].start == 0.25


def test_quantize_time_indices_selection():
    out, res = _apply(_score([_note(0.24, 0.6), _note(1.24, 1.6)]),
                      {"op": "quantize_time", "track": 0, "value": {"grid": 2, "indices": [0]}})
    assert res.ok
    n0, n1 = out.tracks[0].notes
    assert n0.start == 0.25
    assert n1.start == 1.24                              # 未选中不动


def test_quantize_time_zero_length_guard():
    out, res = _apply(_score([_note(0.24, 0.3)]),
                      {"op": "quantize_time", "track": 0, "value": {"grid": 2}})
    assert res.ok
    n = out.tracks[0].notes[0]
    assert n.start == 0.25 and n.end == 0.5              # end<=start → end=start+cell


def test_quantize_time_rejects():
    s = _score([_note(0.24, 0.6)])
    cases = [
        {"grid": 0}, {"grid": -2}, {"grid": "x"}, {"grid": None},
        {"grid": 2, "swing": 1.5}, {"grid": 2, "swing": -0.1}, {"grid": 2, "swing": "x"},
        {"grid": 2, "indices": []}, {"grid": 2, "indices": [7]}, {"grid": 2, "indices": ["x"]},
        "x",
    ]
    for c in cases:
        _out, res = _apply(s, {"op": "quantize_time", "track": 0, "value": c})
        assert res.applied == 0 and res.errors, c


# ---------------- add_track（新建空白 MIDI 轨，#183②）----------------


def test_add_track_default_and_sequence():
    out, res = _apply(_score(), {"op": "add_track", "value": {}})
    assert res.ok and res.applied == 1
    assert len(out.tracks) == 2
    t = out.tracks[1]
    assert t.name == "轨道 2"          # 缺省 = 现轨数 + 1
    assert t.kind == "midi" and t.notes == []


def test_add_track_rename_and_folder():
    out, res = _apply(_score(),
                      {"op": "add_track", "value": {"name": "哼唱", "folder": "人声"}})
    assert res.ok
    t = out.tracks[1]
    assert t.name == "哼唱" and t.folder == "人声"


def test_add_track_dup_name_autonumber():
    out, res = _apply(_score(),                    # 已有轨「轨0」
                      {"op": "add_track", "value": {"name": "轨0"}},
                      {"op": "add_track", "value": {"name": "轨0"}})
    assert res.ok and res.applied == 2
    assert [t.name for t in out.tracks] == ["轨0", "轨0 2", "轨0 3"]


def test_add_track_name_guard():
    _out, res = _apply(_score(), {"op": "add_track", "value": {"name": "x" * 65}})
    assert res.applied == 0 and res.errors
