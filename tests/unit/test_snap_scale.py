"""M-V8 E3 段2 单测：调内吸附——core/snap.py + 命令层 snap_scale op。

语义：
- 判定：调外 且 |deviation_cents| ≥ 42（可调）→ 吸最近调内音（半音级）
- 上下等距（±6 半音，7 声音阶下调外音恒 1/1）按偏差方向：偏高吸上、偏低吸下
- pitch_hz 保留（声学事实）；deviation_cents 按「相对新 pitch_midi」重算（dev -= 100×delta）
- key 缺省自动检测（detect_key；样本 <4 音 → 原样返回）
- 命令层：indices 选区或整轨；bad key/threshold/index → 拒绝（errors 计数）
"""

from __future__ import annotations

import pytest

from tsov.core.key import key_pitch_classes, parse_key
from tsov.core.notes import Note
from tsov.core.score import Score, Track
from tsov.core.snap import nearest_scale_pc, snap_out_of_key
from tsov.host import EditBatch

MAJOR_C = {0, 2, 4, 5, 7, 9, 11}


def _note(start=0.0, end=0.5, pm=60, dev=0.0):
    return Note(start=start, end=end, pitch_midi=pm, pitch_hz=261.6, deviation_cents=dev)


def _score(notes=None):
    return Score(title="t", tracks=[Track(name="轨0", notes=notes or [_note()])])


def _apply(score, *cmds):
    b = EditBatch()
    for c in cmds:
        b.add(**c)
    return b.apply(score)


# ---------------- core：key 解析 ----------------


def test_parse_key_variants():
    assert parse_key("C major") == (0, "major")
    assert parse_key("a minor") == (9, "minor")
    assert parse_key("D dorian") == (2, "dorian")
    assert parse_key("Bb major") == (10, "major")  # 降号容错
    assert parse_key("c# minor") == (1, "minor")
    assert parse_key("xx") is None
    assert parse_key("C lydian") is None  # 未收录调式
    assert parse_key("") is None
    assert sorted(key_pitch_classes("C major")) == sorted(MAJOR_C)
    assert key_pitch_classes("bad") is None


# ---------------- core：最近调内音 ----------------


def test_nearest_scale_pc_tie_by_direction():
    # 7 声音阶下调外音恒为 1/1 等距 → 方向定
    assert nearest_scale_pc(6, MAJOR_C, +50) == (7, 1)   # F# 偏高 → G
    assert nearest_scale_pc(6, MAJOR_C, -50) == (5, -1)  # F# 偏低 → F
    assert nearest_scale_pc(1, MAJOR_C, +50) == (2, 1)
    assert nearest_scale_pc(1, MAJOR_C, -50) == (0, -1)


def test_nearest_scale_pc_non_tie():
    # 非 7 声（假音阶）：取更近侧
    pcs = {0, 4, 7, 11}
    assert nearest_scale_pc(1, pcs, +50) == (0, -1)   # C# → C（距 1）优先于 E（距 3）
    # A(9)：up=B(11) 距 2、dn=G(7) 距 2 → 等距 → 按方向
    assert nearest_scale_pc(9, pcs, +50) == (11, 2)
    assert nearest_scale_pc(9, pcs, -50) == (7, -2)
    assert nearest_scale_pc(5, pcs, +50) == (4, -1)   # F → E（距 1）优先于 G（距 2）


# ---------------- core：吸附 ----------------


def test_snap_explicit_key_basic():
    notes = [
        _note(0.0, 0.5, pm=61, dev=+50),   # C# 偏高 → D，dev 50-100=-50
        _note(0.5, 1.0, pm=66, dev=-50),   # F# 偏低 → F，dev -50+100=+50
        _note(1.0, 1.5, pm=64, dev=+30),   # E 调内 → 不动
    ]
    out, stats = snap_out_of_key(notes, key="C major")
    assert [(n.pitch_midi, n.deviation_cents) for n in out] == [(62, -50.0), (65, 50.0), (64, 30.0)]
    assert stats == {"key": "C major", "auto": False, "scanned": 3, "snapped": 2, "unchanged": 1}
    assert out[0].pitch_hz == notes[0].pitch_hz  # 声学事实保留


def test_snap_threshold_gate():
    # |dev| < 42 → 微偏差不吸
    notes = [_note(pm=61, dev=+30), _note(pm=61, dev=-30)]
    out, stats = snap_out_of_key(notes, key="C major")
    assert [n.pitch_midi for n in out] == [61, 61] and stats["snapped"] == 0


def test_snap_auto_key_and_short_sample():
    # 完整 C 大调音阶（C×3 拉主音权重）+ 1 调外 F# → auto 判 C major → F# 吸到 G
    base = [60, 62, 64, 65, 67, 69, 71, 60, 72]
    notes = [_note(i * .4, i * .4 + .4, pm) for i, pm in enumerate(base)]
    notes.append(_note(4.0, 4.5, pm=66, dev=+45))  # F#(6) 偏高 → G(7)，dev 45-100=-55
    out, stats = snap_out_of_key(notes)
    assert stats["auto"] is True and stats["key"] == "C major"
    assert out[-1].pitch_midi == 67 and out[-1].deviation_cents == pytest.approx(-55.0)
    # 样本不足（<4 音）且未指定 key → 原样
    out2, stats2 = snap_out_of_key([_note(pm=61, dev=+99)])
    assert stats2["key"] is None and out2[0].pitch_midi == 61


def test_snap_bad_key_raises():
    with pytest.raises(ValueError):
        snap_out_of_key([_note(pm=61, dev=+50)], key="C lydian!")


def test_snap_idempotent():
    notes = [_note(pm=61, dev=+50)]
    once, _ = snap_out_of_key(notes, key="C major")
    twice, stats = snap_out_of_key(once, key="C major")
    assert twice[0].pitch_midi == once[0].pitch_midi and stats["snapped"] == 0


# ---------------- 命令层 op ----------------


def test_op_basic_whole_track():
    out, res = _apply(_score([_note(pm=61, dev=+50), _note(pm=64)]),
                      {"op": "snap_scale", "track": 0, "value": {"key": "C major"}})
    assert res.ok and res.applied == 1
    notes = out.tracks[0].notes
    assert notes[0].pitch_midi == 62 and notes[0].deviation_cents == pytest.approx(-50.0)
    assert notes[1].pitch_midi == 64


def test_op_auto_key_and_indices():
    s = _score([_note(pm=61, dev=+50), _note(pm=66, dev=-50), _note(pm=64)])
    # 只吸 index 0（其余不动）
    out, res = _apply(s, {"op": "snap_scale", "track": 0,
                          "value": {"key": "C major", "indices": [0]}})
    assert res.ok and res.applied == 1
    assert [n.pitch_midi for n in out.tracks[0].notes] == [62, 66, 64]


def test_op_rejects():
    s = _score([_note(pm=61, dev=+50)])
    cases = [
        {"value": {"threshold_cents": "x"}},
        {"value": {"threshold_cents": 200}},
        {"value": {"key": "C lydian"}},
        {"value": {"indices": [5]}},
        {"value": "not-a-dict"},
    ]
    for c in cases:
        _out, res = _apply(s, {"op": "snap_scale", "track": 0, **c})
        assert res.applied == 0 and res.errors, c


def test_op_noop_when_all_in_key():
    out, res = _apply(_score([_note(pm=60), _note(pm=64)]),
                      {"op": "snap_scale", "track": 0, "value": {"key": "C major"}})
    assert res.ok  # 无变化不算错误
    assert [n.pitch_midi for n in out.tracks[0].notes] == [60, 64]
