"""P0 A6：三方合并（用户轮中手改 vs agent 结果）。"""

from __future__ import annotations

import copy

from tsov.core.merge import three_way_merge


def _payload():
    return {
        "title": "base", "tempo": 120.0, "time_signature": "4/4", "key_candidates": [],
        "tracks": [{
            "name": "m",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
            "notes": [{"start": 0.0, "end": 0.5, "pitch_midi": 60, "pitch_hz": 261.63,
                       "velocity": 0.8, "confidence": 0.9, "deviation_cents": 0.0, "is_ornament": False}],
        }],
        "meta": {},
    }


def test_disjoint_edits_merge():
    base = _payload()
    cur = copy.deepcopy(base)
    cur["tracks"][0]["notes"][0]["pitch_midi"] = 62        # 用户改音高
    inc = copy.deepcopy(base)
    inc["title"] = "agent 改标题"                          # agent 改标题

    merged, conflicts = three_way_merge(base, cur, inc)
    assert conflicts == []
    assert merged["tracks"][0]["notes"][0]["pitch_midi"] == 62   # 用户改动保留
    assert merged["title"] == "agent 改标题"                      # agent 改动落地


def test_same_position_conflict_keeps_current():
    base = _payload()
    cur = copy.deepcopy(base)
    cur["tracks"][0]["notes"][0]["pitch_midi"] = 62
    inc = copy.deepcopy(base)
    inc["tracks"][0]["notes"][0]["pitch_midi"] = 64

    merged, conflicts = three_way_merge(base, cur, inc)
    assert conflicts and "pitch_midi" in conflicts[0]
    assert merged["tracks"][0]["notes"][0]["pitch_midi"] == 62


def test_agent_append_merges_with_user_edit():
    base = _payload()
    cur = copy.deepcopy(base)
    cur["tracks"][0]["notes"][0]["velocity"] = 0.3
    inc = copy.deepcopy(base)
    inc["tracks"][0]["notes"].append({
        "start": 1.0, "end": 1.5, "pitch_midi": 67, "pitch_hz": 392.0,
        "velocity": 0.8, "confidence": 1.0, "deviation_cents": 0.0, "is_ornament": False,
    })

    merged, conflicts = three_way_merge(base, cur, inc)
    assert conflicts == []
    assert len(merged["tracks"][0]["notes"]) == 2
    assert merged["tracks"][0]["notes"][0]["velocity"] == 0.3


def test_structural_removal_conflicts():
    base = _payload()
    cur = copy.deepcopy(base)
    cur["tracks"][0]["notes"][0]["pitch_midi"] = 62
    inc = copy.deepcopy(base)
    inc["tracks"][0]["notes"] = []

    _, conflicts = three_way_merge(base, cur, inc)
    assert any("notes" in c for c in conflicts)


def test_identical_change_is_not_conflict():
    base = _payload()
    cur = copy.deepcopy(base)
    cur["tempo"] = 140.0
    inc = copy.deepcopy(base)
    inc["tempo"] = 140.0
    merged, conflicts = three_way_merge(base, cur, inc)
    assert conflicts == [] and merged["tempo"] == 140.0
