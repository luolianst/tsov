"""v0.2 批C 后段（挂道族）单测：perf 注册表 + CC/弯音曲线 + MIDI 导出事件。

覆盖：
- curve_specs / perf_specs 域与范围；snapshot.perf 形状
- add_lane 接受 perf 曲线（bend）；velocity 不可绑（bars 伪道）
- set_automation：perf 曲线轨道层通过 + 越界拒绝 + bus/master 拒绝
- score_to_midi：bend → pitch_bends（±8192 映射）、cc1/cc11/cc64 → control_change；
  断点 + 线性加密（时间递增、终值精确）；无 perf 数据 → 零事件（回归）
"""

from __future__ import annotations

import pretty_midi

from tsov.core.notes import Note
from tsov.core.score import Score, Track
from tsov.host.command import EditBatch
from tsov.host.params import curve_specs, perf_specs, snapshot
from tsov.midi.export import score_to_midi


def _score(automation=None) -> Score:
    tr = Track(
        name="t",
        notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6, velocity=0.8)],
        automation=dict(automation or {}),
    )
    return Score(title="t", tempo=120.0, tracks=[tr])


def _apply(score: Score, op: str, value, track: int = 0):
    b = EditBatch()
    b.add(op, track=track, value=value)
    return b.apply(score)


# ---------------- 注册表 ----------------


def test_curve_specs_include_perf_curves():
    cs = curve_specs()
    assert list(cs) == ["volume", "pan", "bend", "cc1", "cc11", "cc64"]
    assert cs["bend"].domain == "perf" and (cs["bend"].lo, cs["bend"].hi) == (-1.0, 1.0)
    assert cs["cc1"].eval_kind == "midi" and cs["cc64"].label == "延音"


def test_perf_specs_velocity_is_bars_pseudo():
    ps = perf_specs()
    assert ps["velocity"].eval_kind == "bars" and ps["velocity"].automatable is False
    snap = snapshot()
    assert snap["perf"]["velocity"]["eval_kind"] == "bars"
    assert snap["perf"]["bend"]["lo"] == -1.0


# ---------------- 命令层 ----------------


def test_add_lane_accepts_bend_rejects_velocity():
    out, res = _apply(_score(), "add_lane", {"param": "bend"})
    assert res.ok and res.applied == 1
    assert out.tracks[0].lanes == [{"id": "l1", "param": "bend"}]
    _o, r2 = _apply(_score(), "add_lane", {"param": "velocity"})
    assert r2.applied == 0 and "未知" in r2.errors[0]


def test_set_automation_perf_curve_track_only():
    out, res = _apply(_score(), "set_automation",
                      {"param": "bend", "points": [[0.0, 0.0], [1.0, 1.0]]})
    assert res.ok and res.applied == 1
    assert out.tracks[0].automation["bend"] == [[0.0, 0.0], [1.0, 1.0]]
    # 越界拒绝（bend ∈ [-1,1]）
    _o, r2 = _apply(_score(), "set_automation", {"param": "bend", "points": [[0, 2.0]]})
    assert r2.applied == 0 and "越界" in r2.errors[0]
    # bus/master 上 perf 拒绝（仅轨道层）
    _o, r3 = _apply(_score(), "set_automation",
                    {"target": "master", "param": "cc1", "points": [[0, 0.5]]})
    assert r3.applied == 0 and "仅支持轨道层" in r3.errors[0]


# ---------------- MIDI 导出 ----------------


def test_midi_export_perf_events(tmp_path):
    s = _score(automation={
        "bend": [[0.0, 0.0], [1.0, 1.0]],
        "cc1": [[0.0, 0.0], [0.5, 1.0]],
        "cc64": [[0.0, 0.0], [0.25, 1.0], [0.5, 0.0]],
    })
    p = tmp_path / "out.mid"
    score_to_midi(s, str(p))
    m = pretty_midi.PrettyMIDI(str(p))
    inst = m.instruments[0]
    # bend：存在且时间递增；终值 1.0 → 8191；加密出中点（>2 断点）
    assert inst.pitch_bends, "bend 事件缺失"
    bt = [e.time for e in inst.pitch_bends]
    assert bt == sorted(bt) and len(bt) > 2
    assert inst.pitch_bends[-1].pitch == 8191
    assert min(e.pitch for e in inst.pitch_bends) >= -8192
    # CC1 / CC64：各自事件 + 峰值 127
    cc1 = [e for e in inst.control_changes if e.number == 1]
    cc64 = [e for e in inst.control_changes if e.number == 64]
    assert cc1 and cc64
    assert max(e.value for e in cc1) == 127
    assert cc64[-1].value == 0
    # 音符不受影响
    assert len(inst.notes) == 1


def test_midi_export_no_perf_zero_events(tmp_path):
    p = tmp_path / "out.mid"
    score_to_midi(_score(), str(p))
    inst = pretty_midi.PrettyMIDI(str(p)).instruments[0]
    assert not inst.pitch_bends and not inst.control_changes
