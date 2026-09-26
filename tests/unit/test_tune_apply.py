"""M-V8 E4 段2 单测：建议→命令 / 预览 / 对拍 / set_effect_params 命令。

- 命令换算：level 以**当前**音量×差量（差量语义——不覆盖手感）；pan 直写；范围守线
- 效果：preset → add_effect 序列；单效果 → add_effect；改参 → set_effect_params（新薄 op）
- 预览：命令层在副本上试跑（原谱零改动）
- 对拍报告：达标统计 / 削波 ❗ / LUFS / 频谱变化
"""

from __future__ import annotations

import pytest

from tsov.core.score import Effect
from tsov.host.command import EditBatch
from tsov.tune import apply as A
from tsov.tune import compare as C

from unit._tune_fixtures import facts_stub, score_two


# ---------------------------------------------------------------------------
# 建议 → 命令
# ---------------------------------------------------------------------------


def test_commands_level_recompute_from_current_volume():
    s = score_two(vol0=0.8)
    cmd = A.suggestion_commands({"kind": "level", "track": 0, "values": {"delta_db": 3.0}}, s)
    assert cmd == [{"op": "set_track_mix", "track": 0, "value": {"volume": 0.5664}}]


def test_commands_pan_and_guards():
    s = score_two()
    assert A.suggestion_commands({"kind": "pan", "track": 1, "values": {"pan": -0.4}}, s) == \
        [{"op": "set_track_mix", "track": 1, "value": {"pan": -0.4}}]
    with pytest.raises(ValueError):
        A.suggestion_commands({"kind": "pan", "track": 0, "values": {"pan": 2.0}}, s)
    with pytest.raises(ValueError):
        A.suggestion_commands({"kind": "zzz", "track": 0, "values": {}}, s)
    with pytest.raises(ValueError):
        A.suggestion_commands({"kind": "level", "track": 9, "values": {"delta_db": -1}}, s)


def test_commands_effect_preset_expands_chain():
    s = score_two()
    cmds = A.suggestion_commands({"kind": "effect", "track": 0,
                                  "effect": {"preset": "piano-pop-reverb"}}, s)
    assert len(cmds) >= 2
    assert all(c["op"] == "add_effect" and c["track"] == 0 for c in cmds)
    with pytest.raises(ValueError):
        A.suggestion_commands({"kind": "effect", "track": 0,
                               "effect": {"preset": "no-such-preset"}}, s)


def test_commands_effect_single_and_set_params():
    s = score_two()
    cmds = A.suggestion_commands(
        {"kind": "effect", "track": 0,
         "effect": {"effect": {"type": "highpass", "params": {"cutoff_frequency_hz": 90}}}}, s)
    assert cmds == [{"op": "add_effect", "track": 0,
                     "value": {"type": "highpass", "params": {"cutoff_frequency_hz": 90}}}]
    cmds2 = A.suggestion_commands(
        {"kind": "effect", "track": 0,
         "effect": {"set_params": {"index": 0, "params": {"wet_level": 0.5}}}}, s)
    assert cmds2 == [{"op": "set_effect_params", "track": 0,
                      "value": {"index": 0, "params": {"wet_level": 0.5}}}]
    with pytest.raises(ValueError):
        A.suggestion_commands({"kind": "effect", "track": 0, "effect": {"nope": 1}}, s)


def test_preview_score_applies_on_copy():
    s = score_two(vol0=1.0)
    new = A.preview_score(s, {"kind": "level", "track": 0, "values": {"delta_db": -6.0}})
    assert abs(new.tracks[0].instrument.volume - 1.9953) < 0.001
    assert s.tracks[0].instrument.volume == 1.0          # 原谱不动


def test_batch_commands_selection_and_meta():
    batch = {"suggestions": [
        {"id": "t1", "kind": "level", "track": 0, "values": {"delta_db": 3.0}},
        {"id": "t2", "kind": "pan", "track": 1, "values": {"pan": -0.35}},
    ]}
    cmds, meta = A.batch_commands(batch, score_two(), ids=["t2"])
    assert meta["ids"] == ["t2"] and meta["by_kind"] == {"pan": 1} and len(cmds) == 1
    _, meta_all = A.batch_commands(batch, score_two())
    assert meta_all["n"] == 2 and meta_all["by_kind"] == {"level": 1, "pan": 1}
    cmds_unknown, meta_unknown = A.batch_commands(batch, score_two(), ids=["zz"])
    assert meta_unknown["n"] == 0 and cmds_unknown == []


# ---------------------------------------------------------------------------
# set_effect_params 命令（E4 段2 薄 op）
# ---------------------------------------------------------------------------


def test_set_effect_params_merge_and_rejects():
    s = score_two(effects0=[Effect(type="reverb", params={"room_size": 0.5, "wet_level": 0.3})])
    eb = EditBatch("t")
    eb.add("set_effect_params", track=0, value={"index": 0, "params": {"wet_level": 0.8}})
    ns, res = eb.apply(s)
    assert res.ok and res.errors == []
    assert ns.tracks[0].instrument.effects[0].params == {"room_size": 0.5, "wet_level": 0.8}

    eb2 = EditBatch("t")
    eb2.add("set_effect_params", track=0, value={"index": 0, "params": {"nope": 1}})
    _, res2 = eb2.apply(s)
    assert not res2.ok and "未知参数键" in res2.errors[0]
    assert s.tracks[0].instrument.effects[0].params["wet_level"] == 0.3   # 原谱零污染

    eb3 = EditBatch("t")
    eb3.add("set_effect_params", track=0, value={"index": 5, "params": {"wet_level": 0.5}})
    _, res3 = eb3.apply(s)
    assert not res3.ok and "越界" in res3.errors[0]

    eb4 = EditBatch("t")
    eb4.add("set_effect_params", track=0, value={"index": 0, "params": {}})
    _, res4 = eb4.apply(s)
    assert not res4.ok and "非空对象" in res4.errors[0]


# ---------------------------------------------------------------------------
# 对拍报告
# ---------------------------------------------------------------------------


def test_build_report_levels_peak_lufs_and_files():
    before = facts_stub([
        {"index": 0, "name": "piano", "rel_db": -10.0},
        {"index": 1, "name": "drums", "rel_db": 0.0},
    ], targets={0: -4.0, 1: 0.0}, mix_peak_dbfs=-0.2,
        spectrum_bands={0: [0.4] * 6, 1: [0.3] * 6})
    after = facts_stub([
        {"index": 0, "name": "piano", "rel_db": -4.5},
        {"index": 1, "name": "drums", "rel_db": 0.0},
    ], targets={0: -4.0, 1: 0.0}, mix_peak_dbfs=-2.5,
        spectrum_bands={0: [0.45] * 6, 1: [0.3] * 6})
    rep = C.build_report(before, after, applied_ids=["t1"], lufs_before=-14.0, lufs_after=-13.4,
                         files={"before": "before.wav", "after": "after.wav"})
    assert rep["ok"] is True and rep["status"] == "ok"
    texts = " | ".join(i["text"] for i in rep["items"])
    assert "电平：2/2 轨到目标带" in texts
    assert "峰值：-0.2 → -2.5 dBFS" in texts
    assert "LUFS：-14.0 → -13.4" in texts
    assert "频谱变化最大：piano" in texts
    lvl = rep["details"]["levels"]
    assert lvl[0]["gap_before"] == -6.0 and lvl[0]["gap_after"] == -0.5 and lvl[0]["ok"] is True
    assert rep["applied"] == ["t1"] and rep["files"]["after"] == "after.wav"


def test_build_report_clipping_is_bad():
    before = facts_stub([{"index": 0, "name": "x", "rel_db": 0.0}], mix_peak_dbfs=-1.5)
    after = facts_stub([{"index": 0, "name": "x", "rel_db": 0.0}], mix_peak_dbfs=1.1, clipping=True)
    rep = C.build_report(before, after)
    assert rep["ok"] is False and rep["status"] == "bad"
    assert any("削波" in i["text"] for i in rep["items"])


def test_build_report_no_lufs_notes_skip():
    f = facts_stub([{"index": 0, "name": "x", "rel_db": 0.0}])
    rep = C.build_report(f, f)
    assert any("未测" in i["text"] for i in rep["items"])
    assert rep["ok"] is True
