"""M-V8 E5 段2 测试：Send 支路 / 总线·master 效果链 / set_automation / vst3 托管。

数值口径（假音源常数信号、SR 8000、手算期望值）与 test_host_mix.py 一致；
回归保护：无 send / 无总线效果时逐位等价旧行为（D1 基线）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tsov.core.notes import Note
from tsov.core.score import Bus, Effect, Instrument, Score, Track
from tsov.host import HostSession, HostTrack, render_buses
from tsov.host.cache import StemStore
from tsov.host.command import EditBatch
from tsov.host.effect import EffectChain, effect_kinds, validate_effect
from tsov.host.instrument import SoundSource

SR = 8000
G6 = 10 ** (-6.0 / 20.0)  # gain_db=-6 → ≈0.5012
_TAL = Path("vendor/vst3/TAL-Chorus-LX.vst3/Contents/x86_64-win/TAL-Chorus-LX.vst3")


class _ConstSource(SoundSource):
    """常数信号源（忽略 notes，方便手算）。"""

    def __init__(self, value: float):
        self.value = float(value)

    def render(self, notes, samplerate, n_frames):
        return np.full((n_frames,), self.value, dtype=np.float32)


def _session(specs, buses=None, master=None):
    score_tracks = []
    host_tracks = []
    for s in specs:
        tr = Track(
            name=s.get("name", "t"),
            instrument=Instrument(program="piano", volume=s.get("volume", 1.0)),
            notes=[Note(start=0.0, end=s.get("end", 1.0), pitch_midi=60, pitch_hz=261.63, velocity=1.0)],
            bus=s.get("bus", "master"),
            pan=s.get("pan", 0.0),
            mute=s.get("mute", False),
            solo=s.get("solo", False),
            automation=s.get("automation", {}),
            sends=s.get("sends", {}),
        )
        score_tracks.append(tr)
        host_tracks.append(HostTrack(track=tr, source=_ConstSource(s.get("value", 0.0))))
    score = Score(
        title="t", tempo=100.0, tracks=score_tracks,
        buses=buses or [], master=master or Bus(name="master"),
    )
    return HostSession(score, host_tracks, samplerate=SR)


def _mid(audio):
    return float(audio[len(audio) // 2])


def _track(name="a"):
    return Track(
        name=name,
        instrument=Instrument(program="piano"),
        notes=[Note(start=0.0, end=1.0, pitch_midi=60, pitch_hz=261.63, velocity=1.0)],
    )


def _score_cmd(tracks=(), buses=(), master=None):
    return Score(
        title="t", tempo=120.0, tracks=list(tracks),
        buses=list(buses), master=master or Bus(name="master"),
    )


def _apply(score, *commands):
    batch = EditBatch("test")
    for c in commands:
        batch.add(**c)
    return batch.apply(score)


# ---------------- 引擎：send 支路 ----------------


def test_empty_sends_bit_exact():
    """无 send / 容器空 → 与旧公式逐位一致（D1 基线回归）。"""
    base = _session(
        [{"value": 0.2, "volume": 1.0}, {"value": 0.3, "volume": 0.5, "bus": "b1"}],
        buses=[Bus(name="b1")],
    )
    with_empty = _session(
        [{"value": 0.2, "volume": 1.0, "sends": {}},
         {"value": 0.3, "volume": 0.5, "bus": "b1", "sends": {"b1": 0.0}}],
        buses=[Bus(name="b1")],
    )
    a = render_buses(base, stereo=True, auto_scale=False)
    b = render_buses(with_empty, stereo=True, auto_scale=False)
    assert np.array_equal(a, b)
    assert abs(float(a[len(a) // 2, 0]) - 0.35) < 1e-6  # 0.2 + 0.3*0.5


def test_send_is_post_fader():
    """send 支路 = post-fader（推子后）：音量 0.5 时支路同样减半。"""
    s = _session([{"value": 0.4, "volume": 1.0, "sends": {"b1": 0.5}}], buses=[Bus(name="b1")])
    assert abs(_mid(render_buses(s, stereo=False, auto_scale=False)) - 0.6) < 1e-6  # 0.4 + 0.2
    s2 = _session([{"value": 0.4, "volume": 0.5, "sends": {"b1": 0.5}}], buses=[Bus(name="b1")])
    assert abs(_mid(render_buses(s2, stereo=False, auto_scale=False)) - 0.3) < 1e-6  # 0.2 + 0.1


def test_send_target_bus_fader_and_master():
    """send 汇入总线后受总线推子；send 到 master 直接汇入。"""
    s = _session([{"value": 0.4, "sends": {"b1": 1.0}}], buses=[Bus(name="b1", volume=0.5)])
    assert abs(_mid(render_buses(s, stereo=False, auto_scale=False)) - 0.6) < 1e-6  # 0.4 + 0.4*0.5
    s2 = _session([{"value": 0.4, "sends": {"master": 0.5}}])
    assert abs(_mid(render_buses(s2, stereo=False, auto_scale=False)) - 0.6) < 1e-6


def test_send_silent_when_muted_or_unsoloed():
    """静音轨 / 非 solo 轨：主路与 send 一起静（不留残响）。"""
    s = _session([{"value": 0.4, "mute": True, "sends": {"b1": 1.0}}], buses=[Bus(name="b1")])
    assert abs(_mid(render_buses(s, stereo=False, auto_scale=False))) < 1e-9
    s2 = _session(
        [{"value": 0.4, "name": "sender", "sends": {"b1": 1.0}},
         {"value": 0.5, "name": "star", "solo": True}],
        buses=[Bus(name="b1")],
    )
    assert abs(_mid(render_buses(s2, stereo=False, auto_scale=False)) - 0.5) < 1e-6


def test_bus_export_includes_send_returns_stems_excluded():
    """buses 导出 = 直连 + 汇入本总线的 send；stems（单轨）不含 send 支路。"""
    sess = _session(
        [{"value": 0.4, "name": "A", "bus": "b1"},
         {"value": 0.2, "name": "B", "bus": "b2", "sends": {"b1": 0.5}}],
        buses=[Bus(name="b1"), Bus(name="b2")],
    )
    bus_audio = render_buses(
        sess, stereo=False, only_bus="b1", include_master_processing=False, auto_scale=False
    )
    assert abs(_mid(bus_audio) - 0.5) < 1e-6  # A(0.4) + B_send(0.1)
    stem_b = render_buses(
        sess, stereo=False, only_track=1, include_bus_processing=False,
        include_master_processing=False, auto_scale=False,
    )
    assert abs(_mid(stem_b) - 0.2) < 1e-6  # 不含 send 支路


# ---------------- 引擎：总线 / master 效果链 ----------------


def test_bus_and_master_effects():
    """总线效果与 master 效果（处理序 效果 → 音量/automation → 声像）。"""
    s = _session(
        [{"value": 0.4, "bus": "b1"}],
        buses=[Bus(name="b1", effects=[Effect(type="gain", params={"gain_db": -6.0})])],
    )
    assert abs(_mid(render_buses(s, stereo=False, auto_scale=False)) - 0.4 * G6) < 1e-3
    s2 = _session(
        [{"value": 0.4}],
        master=Bus(name="master", effects=[Effect(type="gain", params={"gain_db": -6.0})]),
    )
    assert abs(_mid(render_buses(s2, stereo=False, auto_scale=False)) - 0.4 * G6) < 1e-3
    s3 = _session(
        [{"value": 0.4, "bus": "b1"}],
        buses=[Bus(name="b1", effects=[Effect(type="gain", params={"gain_db": -6.0})])],
        master=Bus(name="master", effects=[Effect(type="gain", params={"gain_db": -6.0})]),
    )
    assert abs(_mid(render_buses(s3, stereo=False, auto_scale=False)) - 0.4 * G6 * G6) < 1e-3


def test_bus_effect_tail_extends_render_length():
    """总线/master 效果尾巴纳入渲染长度（reverb room=1 → +5s）。"""
    n0 = len(render_buses(_session([{"value": 0.4, "end": 0.5}]), auto_scale=False))
    s = _session(
        [{"value": 0.4, "end": 0.5}],
        master=Bus(name="master", effects=[Effect(type="reverb", params={"room_size": 1.0})]),
    )
    n1 = len(render_buses(s, auto_scale=False))
    assert n1 - n0 == int(round(5.0 * SR))


def test_sends_do_not_change_stem_key(tmp_path):
    """sends（混音期后段）不进 stem 指纹——D1 基线不因 send 失效。"""
    store = StemStore(tmp_path / "cache")
    tr = Track(
        name="a", instrument=Instrument(program="piano", volume=0.5),
        notes=[Note(start=0.0, end=1.0, pitch_midi=60, pitch_hz=261.63, velocity=1.0)],
        bus="master",
    )
    k1 = store.key_for(tr, SR)
    tr.sends = {"b1": 0.7}
    tr.automation = {"volume": [[0.0, 0.2]]}
    tr.pan = 0.5
    assert store.key_for(tr, SR) == k1


# ---------------- 命令层：sends / 效果 target / set_automation ----------------


def test_set_track_mix_sends_store_and_replace():
    sc = _score_cmd(tracks=[_track()], buses=[Bus(name="b1"), Bus(name="b2")])
    out, res = _apply(sc, {"op": "set_track_mix", "track": 0, "value": {"sends": {"b1": 0.5, "master": 0.0}}})
    assert res.ok and out.tracks[0].sends == {"b1": 0.5}  # 量 0 = 移除不占位
    out2, res2 = _apply(out, {"op": "set_track_mix", "track": 0, "value": {"sends": {"b2": 0.25}}})
    assert res2.ok and out2.tracks[0].sends == {"b2": 0.25}  # 整体替换


def test_set_track_mix_sends_rejects():
    sc = _score_cmd(tracks=[_track()], buses=[Bus(name="b1")])
    _, res = _apply(sc, {"op": "set_track_mix", "track": 0, "value": {"sends": {"ghost": 0.5}}})
    assert not res.ok and "未知总线" in res.errors[0]
    _, res2 = _apply(sc, {"op": "set_track_mix", "track": 0, "value": {"sends": {"b1": 1.5}}})
    assert not res2.ok and "越界" in res2.errors[0]
    _, res3 = _apply(sc, {"op": "set_track_mix", "track": 0, "value": {"sends": [0.5]}})
    assert not res3.ok and "需为对象" in res3.errors[0]


def test_add_remove_effect_target_bus_and_master():
    sc = _score_cmd(tracks=[_track()], buses=[Bus(name="b1")])
    out, res = _apply(sc, {"op": "add_effect", "value": {"type": "gain", "params": {"gain_db": -3}, "target": "bus", "ref": "b1"}})
    assert res.ok and len(out.buses[0].effects) == 1 and out.buses[0].effects[0].params["gain_db"] == -3
    out2, res2 = _apply(out, {"op": "add_effect", "value": {"type": "limiter", "target": "master"}})
    assert res2.ok and len(out2.master.effects) == 1
    _, bad = _apply(sc, {"op": "add_effect", "value": {"type": "gain", "target": "bus", "ref": "ghost"}})
    assert not bad.ok and "未知总线" in bad.errors[0]
    _, bad2 = _apply(sc, {"op": "add_effect", "value": {"type": "gain", "target": "nope"}})
    assert not bad2.ok and "未知 target" in bad2.errors[0]


def test_remove_effect_target_bus():
    bus = Bus(name="b1", effects=[Effect(type="gain", params={"gain_db": -3})])
    sc = _score_cmd(tracks=[_track()], buses=[bus])
    out, res = _apply(sc, {"op": "remove_effect", "value": {"target": "bus", "ref": "b1"}, "index": 0})
    assert res.ok and out.buses[0].effects == []
    _, bad = _apply(sc, {"op": "remove_effect", "value": {"target": "bus", "ref": "b1"}, "index": 1})
    assert not bad.ok and "越界" in bad.errors[0]


def test_set_automation_track_replace_and_clear():
    sc = _score_cmd(tracks=[_track()])
    out, res = _apply(sc, {"op": "set_automation", "track": 0,
                           "value": {"param": "volume", "points": [[0, 0.0], [2, 1.0]]}})
    assert res.ok and out.tracks[0].automation["volume"] == [[0.0, 0.0], [2.0, 1.0]]
    out2, res2 = _apply(out, {"op": "set_automation", "track": 0, "value": {"param": "volume", "points": []}})
    assert res2.ok and "volume" not in out2.tracks[0].automation  # 空数组 = 清除
    out3, res3 = _apply(sc, {"op": "set_automation", "track": 0, "value": {"param": "pan", "points": [[1, -0.5]]}})
    assert res3.ok and out3.tracks[0].automation["pan"] == [[1.0, -0.5]]


def test_set_automation_validation():
    sc = _score_cmd(tracks=[_track()], buses=[Bus(name="b1")])
    cases = [
        ({"param": "volume", "points": [[1, 0.5], [0.5, 0.5]]}, "递增"),
        ({"param": "volume", "points": [[-1, 0.5]]}, "越界"),
        ({"param": "volume", "points": [[0, 3.0]]}, "越界"),
        ({"param": "pan", "points": [[0, 1.5]]}, "越界"),
        ({"param": "tone", "points": [[0, 0.5]]}, "未知 param"),
        ({"param": "volume"}, "points"),
        ({"param": "volume", "points": [[0]]}, "非法"),
        ({"param": "volume", "points": "x"}, "数组"),
    ]
    for value, needle in cases:
        _, res = _apply(sc, {"op": "set_automation", "track": 0, "value": value})
        assert not res.ok and needle in res.errors[0], (value, res.errors)
    _, bad = _apply(sc, {"op": "set_automation",
                         "value": {"target": "bus", "ref": "ghost", "param": "volume", "points": [[0, 1]]}})
    assert not bad.ok and "未知总线" in bad.errors[0]


def test_set_automation_bus_and_master():
    sc = _score_cmd(tracks=[_track()], buses=[Bus(name="b1")])
    out, res = _apply(sc, {"op": "set_automation",
                           "value": {"target": "bus", "ref": "b1", "param": "volume", "points": [[0, 0.5], [1, 1.0]]}})
    assert res.ok and out.buses[0].automation["volume"] == [[0.0, 0.5], [1.0, 1.0]]
    out2, res2 = _apply(out, {"op": "set_automation", "value": {"target": "master", "param": "pan", "points": [[0, -1.0]]}})
    assert res2.ok and out2.master.automation["pan"] == [[0.0, -1.0]]


def test_score_roundtrip_new_fields():
    tr = _track()
    tr.sends = {"b1": 0.5}
    sc = Score(
        title="t", tempo=120.0, tracks=[tr],
        buses=[Bus(name="b1", effects=[Effect(type="gain", params={"gain_db": -3})])],
        master=Bus(name="master", effects=[Effect(type="reverb", params={})]),
    )
    back = Score.from_dict(sc.to_dict())
    assert back.tracks[0].sends == {"b1": 0.5}
    assert back.buses[0].effects[0].type == "gain"
    assert back.master.effects[0].type == "reverb"
    # 旧数据（无 sends/effects 键）→ 缺省（向后兼容）
    old = {
        "title": "t", "tempo": 120.0, "time_signature": "4/4", "key_candidates": [],
        "tracks": [{"name": "a", "instrument": {"backend": "fluidsynth", "program": "", "volume": 1.0, "effects": []},
                    "notes": [], "bus": "master", "pan": 0.0, "mute": False, "solo": False, "automation": {},
                    "folder": "", "kind": "midi", "audio": {}}],
        "meta": {}, "buses": [],
        "master": {"name": "master", "volume": 1.0, "pan": 0.0, "automation": {}},
        "bookmarks": [],
    }
    back2 = Score.from_dict(old)
    assert back2.tracks[0].sends == {} and back2.master.effects == []


# ---------------- vst3 托管 ----------------


def test_vst3_kinds_and_validate():
    assert "vst3" in effect_kinds()
    problems = validate_effect(Effect(type="vst3", params={}))
    assert problems and "path" in problems[0]
    problems2 = validate_effect(Effect(type="vst3", params={"path": "no/such/plugin.vst3"}))
    assert problems2 and "路径不存在" in problems2[0]


def test_vst3_chain_load_failure_is_loud():
    with pytest.raises(ValueError, match="vst3"):
        EffectChain([Effect(type="vst3", params={"path": "no/such/plugin.vst3"})])


@pytest.mark.skipif(not _TAL.is_file(), reason="TAL-Chorus-LX 未就位（vendor/vst3/）")
def test_vst3_chain_real_plugin():
    assert validate_effect(Effect(type="vst3", params={"path": str(_TAL)})) == []
    chain = EffectChain([Effect(type="vst3", params={"path": str(_TAL)})])
    sig = (np.random.RandomState(0).randn(SR, 2) * 0.2).astype(np.float32)  # (frames, 2)：混音层形状约定
    out = chain.process(sig, SR)
    assert out.shape == sig.shape
    assert float(np.max(np.abs(out - sig))) > 1e-4  # 出声且非干声


# ---------------- 总线管理（E5 段2 附：Send 前置） ----------------


def test_add_bus_auto_name_levels_and_dup():
    sc = _score_cmd(tracks=[_track()])
    out, res = _apply(sc, {"op": "add_bus", "value": {}})
    assert res.ok and [b.name for b in out.buses] == ["bus1"]
    out2, res2 = _apply(out, {"op": "add_bus", "value": {}})
    assert res2.ok and [b.name for b in out2.buses] == ["bus1", "bus2"]
    out3, res3 = _apply(out2, {"op": "add_bus", "value": {"name": "drum", "volume": 0.8, "pan": -0.3}})
    assert res3.ok and out3.buses[-1].name == "drum"
    assert abs(out3.buses[-1].volume - 0.8) < 1e-9 and abs(out3.buses[-1].pan + 0.3) < 1e-9
    bad, res4 = _apply(out3, {"op": "add_bus", "value": {"name": "drum"}})
    assert not res4.ok and "已存在" in res4.errors[0]
    bad2, res5 = _apply(out3, {"op": "add_bus", "value": {"name": "master"}})
    assert not res5.ok and "隐式" in res5.errors[0]
    bad3, res6 = _apply(out3, {"op": "add_bus", "value": {"name": "x", "volume": 3.0}})
    assert not res6.ok and "越界" in res6.errors[0]


def test_remove_bus_referenced_guard():
    sc = _score_cmd(tracks=[_track()], buses=[Bus(name="b1"), Bus(name="b2")])
    sc.tracks[0].sends = {"b2": 0.4}
    out, res = _apply(sc, {"op": "remove_bus", "value": {"name": "b1"}})
    assert res.ok and [b.name for b in out.buses] == ["b2"]
    bad, res2 = _apply(out, {"op": "remove_bus", "value": {"name": "b2"}})
    assert not res2.ok and "Send" in res2.errors[0]          # 被 Send 引用 → 拒
    out3, _ = _apply(out, {"op": "set_track_mix", "track": 0, "value": {"sends": {}}})
    out4, res4 = _apply(out3, {"op": "remove_bus", "value": {"name": "b2"}})
    assert res4.ok and out4.buses == []
    # 被轨道路由（track.bus）引用 → 拒
    sc2 = _score_cmd(tracks=[_track()], buses=[Bus(name="b1")])
    sc2.tracks[0].bus = "b1"
    _, res5 = _apply(sc2, {"op": "remove_bus", "value": {"name": "b1"}})
    assert not res5.ok and "路由" in res5.errors[0]
    # 不存在 → 报错
    _, res6 = _apply(sc2, {"op": "remove_bus", "value": {"name": "nope"}})
    assert not res6.ok and "不存在" in res6.errors[0]
