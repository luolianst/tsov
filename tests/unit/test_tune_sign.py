"""P0 A7：tune 电平建议的符号不变量（delta_db 与 after_volume 必须自洽）。"""

from __future__ import annotations

from tsov.tune.suggest import _vol_after, deterministic_suggestions

from unit.test_tune_suggest import _facts_basic


def test_every_level_suggestion_delta_matches_after_volume():
    out = deterministic_suggestions(_facts_basic(mix_peak=1.2, clipping=True))
    levels = [s for s in out if s["kind"] == "level"]
    assert levels, "fixture 应至少产出电平建议"
    for s in levels:
        v = s["values"]
        assert _vol_after(v["before_volume"], v["delta_db"]) == v["after_volume"], s["title"]


def test_clip_suggestion_is_attenuation():
    out = deterministic_suggestions(_facts_basic(mix_peak=1.2, clipping=True))
    clip = [s for s in out if "削波" in s["title"]][0]
    assert clip["values"]["delta_db"] > 0          # 正 = 降（与 _vol_after 同口径）
    assert clip["values"]["after_volume"] < clip["values"]["before_volume"]
