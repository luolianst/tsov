"""v0.2 批C2 段2 单测：Track.lanes（道实体）——物化迁移 / add_lane / remove_lane / 求值分域。

覆盖：
- lanes 序列化三态（缺省 None / [] / 列表 往返同构）
- 物化等价性（现存键全量物化、volume/pan 在前、曲线数据原样保留）
- add_lane 校验（注册表域 / 重复绑定）
- remove_lane 校验（按 id / 按 param / 未找到；移除即失活非破坏）
- 求值分域（active_automation：None=全键 / []=全失活 / 列表=白名单）与乘性级联数值
"""

from __future__ import annotations

import numpy as np

from tsov.core.notes import Note
from tsov.core.score import Score, Track
from tsov.host import EditBatch
from tsov.host.mix import active_automation, apply_track_mix


def _score(tracks: int = 1, automation=None) -> Score:
    ts = []
    for i in range(tracks):
        ts.append(
            Track(
                name=f"轨{i}",
                notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6, velocity=0.8)],
                automation=dict(automation or {}),
            )
        )
    return Score(title="t", tempo=120.0, tracks=ts)


def _apply(score: Score, batch: EditBatch):
    return batch.apply(score)


def _td(**extra):
    d = {
        "name": "a",
        "instrument": {"backend": "fluidsynth", "program": "", "volume": 1.0, "effects": []},
        "notes": [],
        "automation": {},
    }
    d.update(extra)
    return d


# ---------------- lanes 序列化三态 ----------------


def test_lanes_roundtrip_states():
    t = Track.from_dict(_td())  # 缺字段 → None（旧数据同构）
    assert t.lanes is None
    t2 = Track.from_dict(_td(lanes=[]))  # 已管理且清空
    assert t2.lanes == []
    ls = [{"id": "l1", "param": "volume"}, {"id": "l2", "param": "pan", "name": "p"}]
    t3 = Track.from_dict(_td(lanes=ls))
    assert t3.lanes == ls
    assert t3.to_dict()["lanes"] == ls  # 落盘镜像携带


def test_lanes_old_dict_without_key_stays_none():
    """旧 JSON 无 lanes 键 → None；且 to_dict 往返后 from_dict 仍等价。"""
    t = Track.from_dict(_td())
    again = Track.from_dict(t.to_dict())
    assert again.lanes is None


# ---------------- 物化迁移 ----------------


def test_add_lane_materializes_existing_then_appends():
    s = _score(automation={"volume": [[0.0, 1.0], [1.0, 0.5]]})
    out, res = _apply(s, EditBatch().add("add_lane", track=0, value={"param": "pan"}))
    assert res.ok and res.applied == 1
    tr = out.tracks[0]
    assert tr.lanes == [{"id": "l1", "param": "volume"}, {"id": "l2", "param": "pan"}]
    # 非破坏：曲线数据原样保留
    assert tr.automation == {"volume": [[0.0, 1.0], [1.0, 0.5]]}
    # 原 Score 不被修改（事务在深拷贝上）
    assert s.tracks[0].lanes is None


def test_materialize_order_volume_pan_first_then_alpha():
    s = _score(automation={"cc1": [[0, 1]], "volume": [[0, 1]]})
    out, _res = _apply(s, EditBatch().add("add_lane", track=0, value={"param": "pan"}))
    assert [l["param"] for l in out.tracks[0].lanes] == ["volume", "cc1", "pan"]


def test_add_lane_on_managed_empty_stays_empty_plus_new():
    s = _score()
    out1, r1 = _apply(s, EditBatch().add("remove_lane", track=0, value={"param": "volume"}))
    assert r1.applied == 0
    assert out1.tracks[0].lanes is None  # 未找到 → 零副作用（不提前物化）
    out2, res2 = _apply(s, EditBatch().add("add_lane", track=0, value={"param": "volume"}))
    assert res2.ok and out2.tracks[0].lanes == [{"id": "l1", "param": "volume"}]


# ---------------- 校验 ----------------


def test_add_lane_rejects_unknown_and_duplicate():
    out, res = _apply(_score(), EditBatch().add("add_lane", track=0, value={"param": "nope"}))
    assert res.applied == 0 and "未知" in res.errors[0]
    s = _score(automation={"volume": [[0, 1]]})
    _o, r2 = _apply(s, EditBatch().add("add_lane", track=0, value={"param": "volume"}))
    assert r2.applied == 0 and "重复绑定" in r2.errors[0]


def test_remove_lane_by_id_and_param_then_missing():
    s = _score(automation={"volume": [[0, 1]], "pan": [[0, 0.0]]})
    out, res = _apply(s, EditBatch().add("remove_lane", track=0, value={"param": "pan"}))
    assert res.ok
    assert [l["param"] for l in out.tracks[0].lanes] == ["volume"]
    assert out.tracks[0].automation.get("pan") == [[0, 0.0]]  # 数据保留（非破坏）
    out2, res2 = _apply(out, EditBatch().add("remove_lane", track=0, value={"id": "l1"}))
    assert res2.ok and out2.tracks[0].lanes == []
    _o, r3 = _apply(out2, EditBatch().add("remove_lane", track=0, value={"param": "pan"}))
    assert r3.applied == 0 and "未找到" in r3.errors[0]


def test_add_lane_batch_atomic_partial():
    """一批多命令：非法一条不影响其余（部分应用语义与既有命令层一致）。"""
    s = _score()
    out, res = _apply(
        s,
        EditBatch()
        .add("add_lane", track=0, value={"param": "volume"})
        .add("add_lane", track=0, value={"param": "nope"})
        .add("add_lane", track=0, value={"param": "pan"}),
    )
    assert res.applied == 2 and len(res.errors) == 1
    assert [l["param"] for l in out.tracks[0].lanes] == ["volume", "pan"]


# ---------------- 求值分域 ----------------


def test_active_automation_scope():
    tr = Track(name="t")
    tr.automation = {"volume": [[0, 1.0]], "pan": [[0, 0.5]]}
    assert set(active_automation(tr).keys()) == {"volume", "pan"}  # None → 全键（旧工程同构）
    tr.lanes = [{"id": "l1", "param": "volume"}]
    assert set(active_automation(tr).keys()) == {"volume"}  # 白名单
    tr.lanes = []
    assert active_automation(tr) == {}  # 全失活
    tr.lanes = [{"id": "l1", "param": "volume"}, {"id": "l2", "param": "pan"}]
    assert set(active_automation(tr).keys()) == {"volume", "pan"}  # 重绑即复活


def test_apply_track_mix_scope_and_cascade():
    n = 200
    stereo = np.ones((n, 2), dtype=np.float32)
    times = np.arange(n) / 100.0
    xs = np.array([0.0, 1.0])
    vol = np.interp(times, xs, np.array([1.0, 0.5]), left=1.0, right=0.5)
    pan = np.interp(times, xs, np.array([0.0, 0.5]), left=0.0, right=0.5)

    # 未管理：volume+pan 都生效（pan=线性平衡律：对侧衰减、同侧封顶 1「不 boost」）
    tr = Track(name="t")
    tr.automation = {"volume": [[0.0, 1.0], [1.0, 0.5]], "pan": [[0.0, 0.0], [1.0, 0.5]]}
    out = apply_track_mix(stereo.copy(), tr, times)
    lg = np.clip(1 - pan, 0.0, 1.0)
    rg = np.clip(1 + pan, 0.0, 1.0)
    exp = np.stack([vol * lg, vol * rg], axis=1).astype(np.float32)
    assert np.allclose(out, exp, atol=1e-6)

    # 管理为仅 volume：pan 曲线不再参与（缺省 pan=0 → 增益 1/1）
    tr2 = Track(name="t")
    tr2.automation = {"volume": [[0.0, 1.0], [1.0, 0.5]], "pan": [[0.0, 0.0], [1.0, 0.5]]}
    tr2.lanes = [{"id": "l1", "param": "volume"}]
    out2 = apply_track_mix(stereo.copy(), tr2, times)
    exp2 = np.stack([vol, vol], axis=1).astype(np.float32)
    assert np.allclose(out2, exp2, atol=1e-6)

    # 空道：全部失活 → 仅推子（1.0）
    tr3 = Track(name="t")
    tr3.automation = {"volume": [[0.0, 0.5]]}
    tr3.lanes = []
    out3 = apply_track_mix(stereo.copy(), tr3, times)
    assert np.allclose(out3, stereo, atol=1e-6)


def test_no_automation_bitwise_unchanged():
    """无自动化数据工程渲染逐位不变（硬回归）：stereo × 推子，pan 中位增益恒 1。"""
    rng = np.random.default_rng(7)
    stereo = rng.standard_normal((128, 2)).astype(np.float32)
    times = np.arange(128) / 100.0
    tr = Track(name="t")
    tr.instrument.volume = 0.83
    out = apply_track_mix(stereo.copy(), tr, times)
    exp = stereo * np.float32(0.83)
    assert np.array_equal(out, exp)
