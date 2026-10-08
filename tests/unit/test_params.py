"""参数注册表（host/params.py，v0.2 批C 前段 地基件 R1）单测。

口径：单一来源等价性（新 API ≡ 旧表 一字不差）+ 快照形状 + set_automation 错误文案不变。
"""

from __future__ import annotations


from tsov.core.score import Score, Track
from tsov.host import command, effect, params
from tsov.host.command import EditBatch

_EFFECT_KINDS = {
    "reverb", "delay", "compressor", "chorus", "distortion", "gain",
    "highpass", "lowpass", "limiter", "brickwall", "phaser",
}


def _apply(score: Score, op: str, value) -> command.BatchResult:
    b = EditBatch()
    b.add(op, track=0, value=value)
    return b.apply(score)[1]


def test_mix_specs_equivalence():
    """automation_specs() ≡ 历史 _AUTOMATION_PARAMS（值/顺序/文案源）。"""
    specs = params.automation_specs()
    assert list(specs) == ["volume", "pan"]   # 顺序 = 错误文案顺序
    assert (specs["volume"].lo, specs["volume"].hi) == (0.0, 2.0)
    assert (specs["pan"].lo, specs["pan"].hi) == (-1.0, 1.0)
    assert specs["volume"].domain == "mix" and specs["pan"].domain == "mix"
    assert specs["volume"].automatable and specs["pan"].automatable
    assert specs["volume"].eval_kind == "curve" and specs["pan"].eval_kind == "curve"
    assert specs["volume"].label == "音量" and specs["pan"].label == "声像"
    # command 层派生等价（逐键逐值）
    # v0.2 批C 后段：白名单扩至 perf 曲线（bend/cc1/cc11/cc64 追加）
    assert command._AUTOMATION_PARAMS == {"volume": (0.0, 2.0), "pan": (-1.0, 1.0),
                                         "bend": (-1.0, 1.0), "cc1": (0.0, 1.0),
                                         "cc11": (0.0, 1.0), "cc64": (0.0, 1.0)}


def test_effect_ranges_equivalence():
    """effect_param_ranges() ≡ 历史 effect._PARAM_RANGES（同一对象别名，形状完全一致）。"""
    r = params.effect_param_ranges()
    assert effect._PARAM_RANGES is r
    assert set(r) == _EFFECT_KINDS
    assert r["reverb"]["room_size"] == (0.0, 1.0)
    assert r["delay"]["delay_seconds"] == (0.0, 10.0)
    assert r["compressor"]["attack_ms"] == (0.1, 100.0)
    assert r["gain"]["gain_db"] == (-60.0, 24.0)
    assert r["highpass"]["cutoff_frequency_hz"] == (10.0, 20000.0)
    assert r["phaser"]["centre_frequency_hz"] == (20.0, 20000.0)
    # 既有消费面零改动：param_spec / effect_kinds 行为不变
    assert effect.param_spec("reverb")["room_size"] == [0.0, 1.0]
    assert effect.param_spec("nope") == {}
    assert "vst3" in effect.effect_kinds() and len(effect.effect_kinds()) == 12


def test_snapshot_shape():
    s = params.snapshot()
    assert s["automation"]["volume"] == {"label": "音量", "lo": 0.0, "hi": 2.0, "unit": "%", "automatable": True}
    assert s["automation"]["pan"]["lo"] == -1.0 and s["automation"]["pan"]["hi"] == 1.0
    assert s["effects"]["reverb"]["room_size"] == [0.0, 1.0]
    assert s["effects"]["phaser"]["mix"] == [0.0, 1.0]
    assert set(s["effects"]) == _EFFECT_KINDS


def test_set_automation_error_message_unchanged():
    """错误文案同源（R1 兼容条款；v0.2 批C 后段白名单扩至 perf 曲线 → 列表随注册表）。"""
    sc = Score(title="t", tracks=[Track(name="a")])
    r = _apply(sc, "set_automation", {"param": "tone", "points": [[0, 0.5]]})
    assert not r.ok
    assert r.errors[0] == "未知 param：'tone'（volume / pan / bend / cc1 / cc11 / cc64）"
    # 越界文案同源
    r2 = _apply(sc, "set_automation", {"param": "volume", "points": [[0, 3.0]]})
    assert not r2.ok
    assert r2.errors[0] == "set_automation volume 值越界：3.0（0.0~2.0）"
