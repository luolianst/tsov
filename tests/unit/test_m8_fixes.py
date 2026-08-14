"""M8 单轨旋律修复单元测试：重叠裁剪 / 碎段合并 / 调性重算 / 力度真实化。"""

from tsov.analysis.edit import _detect_key
from tsov.core.notes import Note
from tsov.dsp.segment import split_into_segments
from tsov.dsp.transcribe import _fix_overlaps


def _notes(pairs):
    return [Note(start=s, end=e, pitch_midi=p, pitch_hz=0.0) for s, e, p in pairs]


# ---- 重叠修复 ----

def test_fix_overlaps_clips_and_drops_remnants():
    notes = _notes([(0.0, 0.8, 60), (0.6, 1.0, 62), (1.0, 1.01, 64), (1.005, 1.6, 65)])
    fixed = _fix_overlaps(notes)
    # 第一个音裁剪到 0.6（后音 onset）；1.0-1.005 残渣 <30ms 删除
    assert len(fixed) == 3
    assert abs(fixed[0].end - 0.6) < 1e-9
    assert fixed[1].pitch_midi == 62


def test_fix_overlaps_keeps_nonoverlapping():
    notes = _notes([(0.0, 0.5, 60), (0.6, 1.0, 62)])
    fixed = _fix_overlaps(notes)
    assert len(fixed) == 2 and abs(fixed[0].end - 0.5) < 1e-9


# ---- 乐句分割（碎段合并 + 自适应 gap）----

def test_segment_merges_fragments():
    # 用真实 hum05（RMVPE 哼唱，紧密 legato）→ 应切成 2-4 段
    hum05 = [(1.12, 1.26, 42), (1.31, 1.46, 45), (1.46, 1.74, 47), (1.81, 2.08, 47),
             (2.14, 2.31, 47), (2.32, 2.48, 49), (2.48, 2.76, 50), (2.82, 3.09, 50),
             (3.13, 3.30, 50), (3.31, 3.45, 51), (3.50, 3.77, 49), (3.84, 4.16, 49),
             (4.19, 4.40, 45), (4.40, 4.63, 45), (4.65, 5.04, 47)]
    notes = _notes(hum05)
    segs = split_into_segments(notes)
    assert 2 <= len(segs) <= 4, f"segments={len(segs)}"


# ---- 调性重算 ----

def test_detect_key_contains_d_for_dorian():
    # D dorian 旋律（M8：edit 后 key 同步，dorian05 场景）
    notes = [Note(start=i * 0.3, end=i * 0.3 + 0.25, pitch_midi=p, pitch_hz=0.0) for i, p in
             enumerate([41, 45, 47, 47, 47, 48, 50, 50, 50, 52, 48, 48, 45, 45, 47])]
    keys = _detect_key(notes)
    roots = [k.key.split()[0] for k in keys]
    assert "D" in roots, f"key_candidates 应含 D 主音：{keys}"


# ---- 力度真实化 ----

def test_realize_velocity_dynamic():
    from tsov.dsp.backends.game import GameBackend

    backend = GameBackend()
    notes = _notes([(0.0, 0.5, 60), (0.5, 1.5, 62), (1.5, 2.0, 64), (2.0, 4.0, 65), (4.0, 4.2, 66)])
    backend._realize_velocity(notes)
    vels = {round(n.velocity, 2) for n in notes}
    assert len(vels) >= 3
    assert 0.4 <= min(n.velocity for n in notes) <= 0.9
    assert 0.4 <= max(n.velocity for n in notes) <= 0.9
