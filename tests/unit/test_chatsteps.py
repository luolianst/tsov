"""对话产物流（Chat ⇄ Steps）单测：动作计数（tool_stats）/ 轮聚合（_rounds）/ journal stats 字段。

口径：受影响对象数（改 16 个音 = notes:16）；轮聚合按 round（一次指令执行回合）。
约定：不用 pytest tmp_path（handoff 坑 81）——手动 output/<uuid> 目录 + rmtree_force 清理。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from tsov.host.journal import ActionJournal
from tsov.web_actions import tool_stats
from tsov.webapp.routes.retention import _rounds

from unit._cleanup import rmtree_force


def _ws():
    d = Path("output") / f"steps-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# tool_stats —— 按工具语义为主、impact 差算为辅
# ---------------------------------------------------------------------------


def test_tool_stats_notes_by_args_and_impact():
    assert tool_stats("write_notes", {"notes": [1, 2, 3]}, None) == {"notes": 3}
    impact = {"tracks": [{"added": 2, "removed": 1, "changed": 3}]}
    assert tool_stats("duplicate_bars", {}, impact) == {"notes": 6}
    assert tool_stats("edit_score", {}, impact) == {"notes": 6}
    assert tool_stats("voice_to_score", {}, impact) == {"notes": 6}


def test_tool_stats_track_fx_param():
    assert tool_stats("create_track", {"name": "x"}, None) == {"track_add": 1}
    assert tool_stats("remove_track", {"track": 1}, None) == {"track_del": 1}
    assert tool_stats("rename_track", {"track": 0, "name": "a"}, None) == {"track_edit": 1}
    assert tool_stats("apply_effect", {"track": 0, "preset": "p"}, None) == {"fx": 1}
    assert tool_stats("set_track_mix", {"track": 0, "volume": 0.5}, None) == {"param": 1}
    assert tool_stats("set_track_mix", {"track": 0, "volume": 0.5, "pan": 0.2}, None) == {"param": 2}
    assert tool_stats("set_tempo", {"tempo": 90, "time_signature": "3/4"}, None) == {"param": 2}


def test_tool_stats_track_fallback_no_double_count():
    """edit_score 间接加轨 → 兜底计 track_add；专用轨工具不重复计。"""
    imp = {"tracks": [{"instrument": "轨新增", "added": 4, "removed": 0, "changed": 0}]}
    assert tool_stats("edit_score", {}, imp) == {"notes": 4, "track_add": 1}
    assert tool_stats("create_track", {}, imp) == {"track_add": 1}


def test_tool_stats_never_raises():
    assert tool_stats("unknown_tool", None, None) == {"other": 1}
    assert tool_stats("write_notes", {"notes": "oops"}, None) == {"other": 1}
    assert tool_stats("set_tempo", "bad", "bad") == {"other": 1}


# ---------------------------------------------------------------------------
# journal stats 字段（append 写 + 老条目兼容）
# ---------------------------------------------------------------------------


def test_journal_stats_field_and_compat():
    ws = _ws()
    try:
        j = ActionJournal(ws)
        e = j.append(source="agent", label="改谱", stats={"notes": 3})
        assert e["stats"] == {"notes": 3}

        # 缺 stats 的老条目：载入补 {}
        idx_path = ws / ".agent-actions" / "index.json"
        raw = json.loads(idx_path.read_text(encoding="utf-8"))
        raw["entries"][0].pop("stats")
        idx_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        j2 = ActionJournal(ws)
        assert j2.entries[0]["stats"] == {}
    finally:
        rmtree_force(ws)


# ---------------------------------------------------------------------------
# _rounds —— 按对话轮聚合（user 条目不进；stats 合计；stale 计数）
# ---------------------------------------------------------------------------


def test_rounds_aggregation():
    entries = [
        {"seq": 1, "source": "agent", "round": "r1", "label": "改谱",
         "stats": {"notes": 4}, "ts": 100.0, "stale": False},
        {"seq": 2, "source": "agent", "round": "r1", "label": "加效果",
         "stats": {"fx": 1}, "ts": 101.0, "stale": False},
        {"seq": 3, "source": "user", "label": "手势", "ts": 102.0},   # user 不进聚合
        {"seq": 4, "source": "agent", "round": "r2", "label": "改谱",
         "stats": {"notes": 2, "param": 1}, "ts": 103.0, "stale": True},
    ]
    rs = _rounds(entries)
    assert [r["round"] for r in rs] == ["r1", "r2"]
    assert rs[0]["seqs"] == [1, 2] and rs[0]["n"] == 2 and rs[0]["n_stale"] == 0
    assert rs[0]["stats"] == {"notes": 4, "fx": 1}
    assert rs[0]["label_counts"] == {"改谱": 1, "加效果": 1}
    assert rs[0]["ts_first"] == 100.0 and rs[0]["ts_last"] == 101.0
    assert rs[1]["stats"] == {"notes": 2, "param": 1} and rs[1]["n_stale"] == 1


def test_rounds_dirty_stats_tolerated():
    entries = [{"seq": 1, "source": "agent", "round": "r",
                "stats": {"notes": "x", "fx": 2}, "ts": 1.0}]
    rs = _rounds(entries)
    assert rs[0]["stats"] == {"fx": 2}


def test_rounds_empty():
    assert _rounds([]) == []
    assert _rounds([{"seq": 1, "source": "user"}]) == []
