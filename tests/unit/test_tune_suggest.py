"""M-V8 E4 段2 单测：建议引擎（确定性 / LLM 校验 / 合并 / 批次）。

红线断言：
- 确定性数值 = 计算（10^(−Δ/20) 换算；封顶 ±12 dB/步）
- LLM 数值必过校验；不过 = 丢弃原文（绝不「修复」）+ why 记因；通道整体失败 = 重试一次→丢通道
- 理由：LLM 优先，缺席公式兜底
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from tsov.tune import store, suggest

from unit._tune_fixtures import facts_stub


def _facts_basic(*, piano_rel=-10.0, drums_rel=0.0, vol0=1.0, mix_peak=-6.0, clipping=False,
                 pan0=0.0, pan1=0.0, bands0=None, bands1=None, notes0=12, notes1=12):
    return facts_stub([
        {"index": 0, "name": "piano", "rel_db": piano_rel, "volume": vol0, "pan": pan0,
         "notes": notes0, "families": ["piano"]},
        {"index": 1, "name": "drums", "rel_db": drums_rel, "volume": 1.0, "pan": pan1,
         "notes": notes1, "families": ["drum"]},
    ], targets={0: -4.0, 1: 0.0}, mix_peak_dbfs=mix_peak, clipping=clipping,
        spectrum_bands={0: bands0, 1: bands1} if bands0 else None)


def _facts_pan_pair(*, fam1="guitar", pan0=0.0, pan1=0.0, rel1=0.0):
    return facts_stub([
        {"index": 0, "name": "piano", "rel_db": -2.0, "pan": pan0, "notes": 12,
         "families": ["piano"], "volume": 1.0},
        {"index": 1, "name": "gt", "rel_db": rel1, "pan": pan1, "notes": 16,
         "families": [fam1], "volume": 1.0},
    ], targets={0: -2.0, 1: -2.0},
        spectrum_bands={0: [0.1, 0.2, 0.3, 0.2, 0.1, 0.1], 1: [0.1, 0.2, 0.3, 0.2, 0.1, 0.1]})


# ---------------------------------------------------------------------------
# 确定性通道
# ---------------------------------------------------------------------------


def test_det_level_diff_volume_and_evidence():
    out = suggest.deterministic_suggestions(_facts_basic())   # piano -10 vs 目标 -4 → +6
    lv = [s for s in out if s["kind"] == "level" and s["track"] == 0]
    assert len(lv) == 1
    v = lv[0]["values"]
    assert v["delta_db"] == -6.0 and v["raw_diff_db"] == -6.0   # 负 = 提升（音量×10^(6/20)）
    assert v["target_rel_db"] == -4.0 and v["measured_rel_db"] == -10.0
    assert abs(v["after_volume"] - 1.9953) < 0.001 and v["capped"] is False
    assert "levels.tracks[0].rel_db" in lv[0]["evidence"]["refs"]
    assert "targets.per_track.0.level_hint_db" in lv[0]["evidence"]["refs"]


def test_det_too_loud_reduces_volume():
    # piano 反比鼓高 4dB（-1 vs -5），目标低 4dB → diff +8 → 降 8
    out = suggest.deterministic_suggestions(_facts_basic(piano_rel=-1.0, drums_rel=-5.0, vol0=0.8))
    lv = [s for s in out if s["kind"] == "level" and s["track"] == 0][0]
    assert lv["values"]["delta_db"] == 8.0
    assert abs(lv["values"]["after_volume"] - 0.3184) < 0.001


def test_det_skip_within_threshold():
    out = suggest.deterministic_suggestions(_facts_basic(piano_rel=-5.0))  # 差 1.0 < 1.5
    assert [s for s in out if s["kind"] == "level"] == []


def test_det_cap_12db_per_step():
    out = suggest.deterministic_suggestions(_facts_basic(piano_rel=-30.0))  # 差 -26 → 封顶 -12
    lv = [s for s in out if s["kind"] == "level" and s["track"] == 0][0]
    assert lv["values"]["delta_db"] == -12.0 and lv["values"]["capped"] is True
    assert lv["values"]["raw_diff_db"] == -26.0
    assert lv["values"]["after_volume"] == 2.0   # 10^(12/20)≈3.98 → 夹到 2.0


def test_det_clipping_suggestion():
    out = suggest.deterministic_suggestions(_facts_basic(mix_peak=1.2, clipping=True))
    clip = [s for s in out if "削波" in s["title"]]
    assert len(clip) == 1
    assert clip[0]["track"] == 1                 # 最响轨（drums）
    assert clip[0]["values"]["delta_db"] == -2.2
    assert clip[0]["evidence"]["refs"] == ["levels.mix_peak_dbfs"]


def test_det_pan_collision_pair_spread():
    out = suggest.deterministic_suggestions(_facts_pan_pair())
    pans = [s for s in out if s["kind"] == "pan"]
    assert len(pans) == 2
    by = {s["track"]: s["values"]["pan"] for s in pans}
    assert by == {0: -0.35, 1: 0.35}
    assert pans[0]["evidence"]["refs"][0].startswith("spectrum.tracks[")


def test_det_pan_skips_drums_and_offcenter():
    out = suggest.deterministic_suggestions(_facts_basic(bands0=[0.2] * 6, bands1=[0.2] * 6))
    assert [s for s in out if s["kind"] == "pan"] == []          # drums 不参与
    out2 = suggest.deterministic_suggestions(_facts_pan_pair(pan1=0.5))
    assert [s for s in out2 if s["kind"] == "pan"] == []         # 已摆开


# ---------------------------------------------------------------------------
# LLM 校验（绝不修复数值）
# ---------------------------------------------------------------------------


def _facts_llm():
    return facts_stub([
        {"index": 0, "name": "piano", "rel_db": -10.0, "volume": 1.0,
         "effects": [{"index": 0, "type": "reverb", "params": {"room_size": 0.5}}]},
        {"index": 1, "name": "drums", "rel_db": 0.0},
    ], targets={0: -4.0, 1: 0.0})


def test_validate_llm_accepts_all_shapes():
    raw = [
        {"kind": "level", "track": 0, "delta_db": -2.5, "title": "t", "reason": "r", "evidence": "e"},
        {"kind": "pan", "track": 0, "pan": -0.4, "reason": "r"},
        {"kind": "effect", "track": 0, "preset": "piano-pop-reverb", "reason": "r"},
        {"kind": "effect", "track": 0,
         "effect": {"type": "highpass", "params": {"cutoff_frequency_hz": 90}}, "reason": "r"},
        {"kind": "effect", "track": 0, "set_params": {"index": 0, "params": {"wet_level": 0.5}},
         "reason": "r"},
    ]
    valid, dropped = suggest.validate_llm_suggestions(raw, _facts_llm())
    assert len(valid) == 5 and dropped == []
    assert abs(valid[0]["values"]["after_volume"] - 1.3335) < 0.001   # 10^(2.5/20)
    assert valid[2]["effect"]["preset"] == "piano-pop-reverb"
    assert valid[3]["effect"]["effect"]["type"] == "highpass"
    assert valid[4]["effect"]["set_params"]["index"] == 0


def test_validate_llm_rejects_keep_raw_and_why():
    raw = [
        {"kind": "warp", "track": 0, "reason": "r"},
        {"kind": "level", "track": 9, "delta_db": -1, "reason": "r"},
        {"kind": "pan", "track": 0, "pan": 2.5, "reason": "r"},
        {"kind": "effect", "track": 0, "preset": "nope-preset", "reason": "r"},
        {"kind": "effect", "track": 0,
         "effect": {"type": "highpass", "params": {"cutoff_frequency_hz": 99000}}, "reason": "r"},
        {"kind": "effect", "track": 0,
         "effect": {"type": "highpass", "params": {"nope": 1}}, "reason": "r"},
        {"kind": "effect", "track": 0, "set_params": {"index": 3, "params": {"wet_level": 0.5}},
         "reason": "r"},
        {"kind": "level", "track": 1, "delta_db": -1, "reason": ""},
        {"kind": "pan", "track": 0, "pan": 0.0, "reason": "r"},
    ]
    valid, dropped = suggest.validate_llm_suggestions(raw, _facts_llm())
    assert valid == [] and len(dropped) == 9
    assert dropped[0]["raw"] is raw[0]               # 原文留存，绝不「修复」
    why = " | ".join(str(d["why"]) for d in dropped)
    for frag in ("未知 kind", "track 不存在", "pan 越界", "未知效果预设", "越界",
                 "未知参数键", "set_params.index 越界", "缺 reason", "无实际变化"):
        assert frag in why, frag


# ---------------------------------------------------------------------------
# LLM 通道（重试语义 / 降级）
# ---------------------------------------------------------------------------


def test_llm_retry_once_then_success():
    calls = {"n": 0}

    def post(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return "不是 JSON"
        return json.dumps({"reasons": [{"i": 0, "reason": "因为事实"}],
                           "suggestions": [{"kind": "pan", "track": 0, "pan": -0.4,
                                            "reason": "空间"}]})

    det = [{"source": "det", "kind": "level", "track": 0, "values": {}, "title": "x",
            "evidence": {}}]
    out = suggest.llm_suggestions(_facts_llm(), det, key="k", post=post)
    assert out["attempts"] == 2 and len(out["suggestions"]) == 1
    assert out["reasons"] == {0: "因为事实"}
    assert any("不合形状" in e for e in out["errors"])


def test_llm_two_failures_drops_channel():
    out = suggest.llm_suggestions(_facts_llm(), [], key="k", post=lambda p: "junk")
    assert out["suggestions"] == [] and out["attempts"] == 2 and len(out["errors"]) == 2


def test_llm_no_key_skips_channel():
    out = suggest.llm_suggestions(_facts_llm(), [], key="", post=None)
    assert out["attempts"] == 0 and out["suggestions"] == []
    assert "无 API key" in out["errors"][0]


def test_merge_reasons_and_formula_fallback():
    det = [{"source": "det", "kind": "level", "track": 0, "title": "x",
            "values": {"delta_db": 6.0, "raw_diff_db": -6.0, "target_rel_db": -4.0,
                       "measured_rel_db": -10.0, "before_volume": 1.0, "after_volume": 1.9953,
                       "capped": False},
            "evidence": {"refs": [], "text": ""}},
           {"source": "det", "kind": "pan", "track": 1, "title": "y",
            "values": {"pan": -0.35, "before_pan": 0.0}, "evidence": {"refs": [], "text": ""}}]
    llm = {"suggestions": [{"source": "llm", "kind": "pan", "track": 0, "title": "z",
                            "reason": "R", "evidence": {}, "values": {"pan": 0.4,
                                                                      "before_pan": 0.0}}],
           "reasons": {0: "LLM 写的理由"}}
    merged = suggest.merge_suggestions(det, llm)
    assert [s["id"] for s in merged] == ["t1", "t2", "t3"]
    assert merged[0]["reason"] == "LLM 写的理由"
    assert merged[1]["reason"].startswith("事实：")
    assert merged[2]["reason"] == "R"


def test_generate_batch_persists_and_events():
    from unit._cleanup import rmtree_force

    d = Path("output") / f"tunetest-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        batch = suggest.generate_batch(d, None, name="t", facts=_facts_llm(), key="", post=None)
        assert batch["batch_ts"] and batch["state"] == "pending"
        assert (d / "tune" / f"{batch['batch_ts']}.json").is_file()
        evs = store.load_events(d, type_="suggest")
        assert len(evs) == 1 and evs[0]["batch_ts"] == batch["batch_ts"]
        assert any(s["kind"] == "level" for s in batch["suggestions"])
        assert batch["stats"]["llm_attempts"] == 0
        assert "无 API key" in batch["stats"]["llm_errors"][0]
    finally:
        rmtree_force(d)
