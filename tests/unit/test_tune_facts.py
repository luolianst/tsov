"""M-V8 E4 段2 单测：事实包（目标解析 / build_facts 装配 / 同极换算）。

- 目标解析：gm_program 精确 → 名称关键词兜底；包缺失 → ValueError；无包 → 通用平衡口径
- build_facts：一遍渲染循环（注入假渲染器）→ 结构/电平/频谱/逐段 schema 齐全
- 同极换算：target_relatives / measured_relative（锚=0 dB 档角色轨 → 最响绑定轨）
"""

from __future__ import annotations

import pytest

from tsov.core.score import Instrument, Track
from tsov.tune import facts as F

from unit._tune_fixtures import FakeRenderer, SR, facts_stub, score_two, tone


# ---------------------------------------------------------------------------
# 目标解析
# ---------------------------------------------------------------------------


def test_resolve_targets_pack_gm_match_and_name_fallback():
    s = score_two(prog0="0", prog1="")   # piano=gm0 精确；drums 无 gm → 名称兜底
    t = F.resolve_targets(s, "pop-band-standard")
    per = t["per_track"]
    assert t["source"] == "pack:pop-band-standard" and t["mix_notes"]
    assert per["0"]["role"] == "piano" and per["0"]["via"] == "gm"
    assert per["0"]["level_hint_db"] == -5.0
    assert per["1"]["role"] == "e_drums" and per["1"]["via"] == "name"


def test_resolve_targets_name_fallback_chinese():
    s = score_two(name0="弦乐 pad", prog0="", name1="贝斯", prog1="")
    t = F.resolve_targets(s, "pop-band-standard")
    assert t["per_track"]["0"]["role"] == "strings_pad"
    assert t["per_track"]["1"]["role"] == "bass"


def test_resolve_targets_pack_missing_raises():
    with pytest.raises(ValueError):
        F.resolve_targets(score_two(), "no-such-pack")


def test_resolve_targets_generic_only_matched():
    s = score_two(name0="钢琴 L", prog0="", name1="神秘层", prog1="")
    t = F.resolve_targets(s, None)
    assert t["source"] == "generic"
    assert t["per_track"]["0"]["level_hint_db"] == -6.0
    assert 1 in t["unmatched"]


# ---------------------------------------------------------------------------
# 同极换算
# ---------------------------------------------------------------------------


def test_target_relatives_pole_zero_role_and_measured():
    facts = facts_stub([
        {"index": 0, "name": "piano", "rel_db": -6.0},   # 相对最响轨（drums）
        {"index": 1, "name": "drums", "rel_db": 0.0},
    ], targets={0: -4.0, 1: 0.0})
    targets, pole = F.target_relatives(facts)
    assert pole == 1
    assert targets == {0: -4.0, 1: 0.0}
    assert F.measured_relative(facts, 0, pole) == -6.0
    assert F.measured_relative(facts, 1, pole) == 0.0


def test_target_relatives_empty_without_targets_or_pole():
    facts = facts_stub([{"index": 0, "name": "x", "rel_db": 0.0}], targets=None)
    assert F.target_relatives(facts) == ({}, None)
    assert F.measured_relative(facts, 0, None) is None


def test_target_relatives_anchor_not_zero_role():
    """最响轨不是 0 dB 档角色时：极轨仍选 0 dB 档（目标相对值按它换算）。"""
    facts = facts_stub([
        {"index": 0, "name": "pad", "rel_db": 0.0},      # 当前最响（混得不对）
        {"index": 1, "name": "drums", "rel_db": -8.0},
    ], targets={0: -18.0, 1: 0.0})
    targets, pole = F.target_relatives(facts)
    assert pole == 1                                  # 0 dB 档优先
    assert targets == {0: -18.0, 1: 0.0}
    assert F.measured_relative(facts, 0, pole) == 8.0  # pad 比鼓高 8 dB


# ---------------------------------------------------------------------------
# build_facts 装配
# ---------------------------------------------------------------------------


def test_build_facts_schema_sections_levels_spectrum():
    s = score_two(sections=[("A", 0.0, 1.0), ("B", 1.0, 2.0)])
    mix = tone(300, 2.0, 0.5) + tone(80, 2.0, 0.1)
    r = FakeRenderer({0: tone(300, 2.0, 0.5), 1: tone(80, 2.0, 0.1)}, mix=mix)
    f = F.build_facts("out/none", s, name="t1", renderer=r, ts=123.0)

    assert r.closed is False                     # 注入渲染器不归 build_facts 关
    assert f["built_at"] == 123.0 and f["project"] == "t1"
    st = f["structure"]
    assert st["samplerate"] == SR and st["notes_total"] == 24
    assert st["sections"] == [{"label": "A", "start": 0.0, "end": 1.0},
                              {"label": "B", "start": 1.0, "end": 2.0}]
    assert st["tracks"][0]["families"] == ["piano"]
    assert "effects" in st["tracks"][0] and st["tracks"][0]["volume"] == 1.0

    lv = f["levels"]
    assert lv["anchor"] == 0 and lv["tracks"][0]["rel_db"] == 0.0
    assert lv["tracks"][1]["rel_db"] < -6        # 0.1 vs 0.5
    assert lv["mix_peak_dbfs"] > -6              # 0.6 峰值 ≈ -4.4 dBFS
    assert lv["tracks"][0]["loudest_win_dbfs"] is not None

    sp = f["spectrum"]
    assert len(sp["tracks"][0]["bands"]) == 6
    assert sp["tracks"][0]["bands"][1] > 0.9     # 300 Hz → 中低段
    assert sp["tracks"][1]["bands"][0] > 0.9     # 80 Hz → 低段
    assert sp["tracks"][0]["centroid_hz"] > 0

    assert len(f["sections_levels"]) == 4        # 2 段 × 2 轨
    row = next(x for x in f["sections_levels"] if x["track"] == 0 and x["label"] == "A")
    assert row["rms_dbfs"] < 0


def test_build_facts_empty_track_and_renderer_close_own():
    s = score_two()
    s.tracks.append(Track(name="空轨", instrument=Instrument(), notes=[]))
    r = FakeRenderer({0: tone(300, 1.0, 0.5), 1: tone(80, 1.0, 0.3)})
    f = F.build_facts("out/none", s, renderer=r, ts=1.0)
    row2 = f["levels"]["tracks"][2]
    assert row2["rms_dbfs"] is None and row2["silent"] is True and row2["rel_db"] is None
    assert f["spectrum"]["tracks"][2]["bands"] == [0.0] * 6
    assert r.closed is False                     # 仍是注入渲染器
