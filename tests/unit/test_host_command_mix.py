"""UI 批A 命令层扩展单测：set_tempo / set_track_mix / set_instrument / add_effect / remove_effect。

（骨架 v3 参数层的最小集；与 agent 工具 set_tempo / set_track_mix 同语义走同一通道）
"""

from __future__ import annotations

from tsov.core.notes import Note
from tsov.core.score import Effect, Score, Track
from tsov.host import EditBatch


def _score(tracks: int = 1) -> Score:
    ts = []
    for i in range(tracks):
        ts.append(
            Track(
                name=f"轨{i}",
                notes=[Note(start=0.0, end=0.5, pitch_midi=60, pitch_hz=261.6, velocity=0.8)],
            )
        )
    return Score(title="t", tempo=120.0, tracks=ts)


def _apply(score: Score, batch: EditBatch):
    out, res = batch.apply(score)
    return out, res


# ---------------- set_tempo（工程级）----------------


def test_set_tempo_only_tempo():
    out, res = _apply(_score(), EditBatch().add("set_tempo", value={"tempo": 146.4}))
    assert res.ok and res.applied == 1
    assert out.tempo == 146.4


def test_set_tempo_time_signature_only():
    out, res = _apply(_score(), EditBatch().add("set_tempo", value={"time_signature": "6/8"}))
    assert res.ok and out.time_signature == "6/8"
    assert out.tempo == 120.0  # tempo 不动


def test_set_tempo_both():
    out, res = _apply(_score(), EditBatch().add("set_tempo", value={"tempo": 200, "time_signature": "3/4"}))
    assert res.ok and out.tempo == 200.0 and out.time_signature == "3/4"


def test_set_tempo_rejects():
    cases = [
        {"value": {"tempo": 500}},            # 越界
        {"value": {"tempo": "fast"}},         # 非法
        {"value": {"time_signature": "7/9"}}, # 非法拍号
        {"value": {}},                        # 空
        {"value": "146"},                     # 非 dict
    ]
    for c in cases:
        _out, res = _apply(_score(), EditBatch().add("set_tempo", value=c["value"]))
        assert res.applied == 0 and res.errors, c


def test_set_tempo_works_on_empty_track_list():
    """工程级命令不要求有轨道（_apply_one 的 track 越界检查前处理）。"""
    s = Score(title="empty", tempo=100.0, tracks=[])
    out, res = _apply(s, EditBatch().add("set_tempo", value={"tempo": 90}))
    assert res.ok and out.tempo == 90.0


# ---------------- set_track_mix ----------------

def test_set_track_mix_volume_zero_survives():
    """坑：volume=0 不能被 `x or 默认` 顶回 1.0（M-V4 教训的回归测试）。"""
    out, res = _apply(_score(), EditBatch().add("set_track_mix", track=0, value={"volume": 0.0}))
    assert res.ok and out.tracks[0].instrument.volume == 0.0


def test_set_track_mix_full():
    out, res = _apply(
        _score(),
        EditBatch().add("set_track_mix", track=0, value={"volume": 0.73, "pan": -0.4, "mute": True, "solo": True}),
    )
    assert res.ok
    tr = out.tracks[0]
    assert tr.instrument.volume == 0.73 and tr.pan == -0.4 and tr.mute is True and tr.solo is True


def test_set_track_mix_bus_ok_and_reject():
    out, res = _apply(_score(), EditBatch().add("set_track_mix", track=0, value={"bus": "master"}))
    assert res.ok and out.tracks[0].bus == "master"
    _out2, res2 = _apply(_score(), EditBatch().add("set_track_mix", track=0, value={"bus": "nope"}))
    assert res2.applied == 0 and "未知总线" in res2.errors[0]


def test_set_track_mix_rejects():
    for value in ({"volume": 3.0}, {"pan": -1.5}, {"bogus": 1}, "x"):
        _out, res = _apply(_score(), EditBatch().add("set_track_mix", track=0, value=value))
        assert res.applied == 0 and res.errors, value


def test_set_track_mix_track_bounds():
    _out, res = _apply(_score(1), EditBatch().add("set_track_mix", track=5, value={"volume": 0.5}))
    assert res.applied == 0 and "越界" in res.errors[0]


# ---------------- set_instrument ----------------

def test_set_instrument_gm_name():
    out, res = _apply(_score(), EditBatch().add("set_instrument", track=0, value={"program": "piano"}))
    assert res.ok and out.tracks[0].instrument.program == "piano"


def test_set_instrument_vst3_path_and_empty():
    out, res = _apply(
        _score(),
        EditBatch()
        .add("set_instrument", track=0, value={"program": "vst3:vendor/vst3/Dexed.vst3/Contents/x86_64-win/Dexed.vst3"})
        .add("set_instrument", track=0, value={"program": ""}),
    )
    assert res.ok and out.tracks[0].instrument.program == ""


def test_set_instrument_rejects_unknown_name():
    _out, res = _apply(_score(), EditBatch().add("set_instrument", track=0, value={"program": "nonsense"}))
    assert res.applied == 0 and "未知音色名" in res.errors[0]


# ---------------- add_effect / remove_effect ----------------

def test_add_effect_default_params_ok():
    out, res = _apply(_score(), EditBatch().add("add_effect", track=0, value={"type": "reverb"}))
    assert res.ok and len(out.tracks[0].instrument.effects) == 1
    assert out.tracks[0].instrument.effects[0].type == "reverb"


def test_add_effect_with_params():
    out, res = _apply(
        _score(),
        EditBatch().add("add_effect", track=0, value={"type": "gain", "params": {"gain_db": 3.0}}),
    )
    assert res.ok and out.tracks[0].instrument.effects[0].params == {"gain_db": 3.0}


def test_add_effect_rejects():
    for value in ({"type": "bitcrusher"}, {"type": "reverb", "params": {"nope": 1}}, {"params": {}}, "x"):
        _out, res = _apply(_score(), EditBatch().add("add_effect", track=0, value=value))
        assert res.applied == 0 and res.errors, value


def test_remove_effect_index_and_bounds():
    s = _score()
    s.tracks[0].instrument.effects.append(Effect(type="gain"))
    s.tracks[0].instrument.effects.append(Effect(type="reverb"))
    out, res = _apply(s, EditBatch().add("remove_effect", track=0, index=0))
    assert res.ok and [e.type for e in out.tracks[0].instrument.effects] == ["reverb"]
    _out2, res2 = _apply(s, EditBatch().add("remove_effect", track=0, index=9))
    assert res2.applied == 0 and "越界" in res2.errors[0]


def test_mixed_batch_partial_apply():
    """部分应用语义：合法生效 + 非法进 errors（既有契约不回归）。"""
    out, res = _apply(
        _score(),
        EditBatch()
        .add("set_track_mix", track=0, value={"volume": 0.6})
        .add("set_track_mix", track=0, value={"pan": 9})
        .add("set_tempo", value={"tempo": 140}),
    )
    assert res.applied == 2 and len(res.errors) == 1
    assert out.tracks[0].instrument.volume == 0.6 and out.tempo == 140.0
