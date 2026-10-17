"""P0 A3：单命令 savepoint——被拒命令不得留下半改状态。"""

from __future__ import annotations

from tsov.core.score import Score
from tsov.host.command import EditBatch


def _score():
    return Score.from_dict({
        "title": "s", "tempo": 120.0, "time_signature": "4/4", "key_candidates": [],
        "tracks": [{
            "name": "m",
            "instrument": {"backend": "fluidsynth", "program": "piano", "volume": 0.8, "effects": []},
            "notes": [],
        }],
        "meta": {},
    })


def test_set_tempo_rejects_without_partial_write():
    """tempo 合法 + 拍号非法 → 整条拒绝，tempo 不得先改。"""
    new, res = EditBatch().add("set_tempo", value={"tempo": 150, "time_signature": "6/7"}).apply(_score())
    assert res.applied == 0 and res.errors
    assert new.tempo == 120.0
    assert new.time_signature == "4/4"


def test_set_track_mix_rejects_without_partial_write():
    """volume 合法 + pan 非法 → volume 不得先改。"""
    new, res = EditBatch().add("set_track_mix", track=0, value={"volume": 0.3, "pan": 5}).apply(_score())
    assert res.applied == 0 and res.errors
    assert new.tracks[0].instrument.volume == 0.8
    assert new.tracks[0].pan == 0.0


def test_set_track_mix_rejects_bad_sends_without_partial_write():
    new, res = EditBatch().add("set_track_mix", track=0,
                               value={"volume": 0.3, "sends": {"ghost": 1}}).apply(_score())
    assert res.applied == 0 and res.errors
    assert new.tracks[0].instrument.volume == 0.8
    assert new.tracks[0].sends == {}


def test_batch_partial_apply_keeps_good_command_only():
    b = EditBatch()
    b.add("set_tempo", value={"tempo": 150, "time_signature": "3/4"})
    b.add("set_track_mix", track=0, value={"volume": 0.2, "pan": 9})
    new, res = b.apply(_score())
    assert res.applied == 1 and len(res.errors) == 1
    assert new.tempo == 150 and new.time_signature == "3/4"
    assert new.tracks[0].instrument.volume == 0.8   # 被拒命令零副作用
