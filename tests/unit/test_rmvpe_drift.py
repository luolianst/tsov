"""RMVPE legato 漂移切分（_split_on_drift）单元测试。

覆盖多级稳定音高漂移（M3-FIX 场景）：一段 legato 内连续两次音高漂移时，
切分边界必须按段内索引正确换算（回归：M4 恢复时全局/局部索引混用导致第二次边界后移）。
纯函数测试，不加载 RMVPE 模型、不读音频。
"""

import numpy as np

from tsov.dsp.backends.rmvpe import DEFAULTS, _split_on_drift
from tsov.dsp.pitch import hz_to_midi, midi_to_hz


def _smooth_levels(levels, hold=12, glide=4, step_s=0.01):
    """构造 3 级稳定音高 + 平滑滑音的帧级序列。

    levels: 稳定级列表（midi）；hold: 每级稳定帧数；glide: 级间滑音帧数。
    返回 (time, pitch_hz, midi_seq)。
    """
    midi = []
    for i, lv in enumerate(levels):
        midi += [float(lv)] * hold
        if i < len(levels) - 1:
            gl = np.linspace(lv, levels[i + 1], glide + 2)[1:-1]
            midi += [float(x) for x in gl]
    time = np.arange(len(midi)) * step_s
    hz = np.array([midi_to_hz(x) for x in midi])
    return time, hz, midi


def test_split_on_drift_multiple_levels_keep_global_bounds():
    time, hz, midi = _smooth_levels([69.0, 71.0, 74.0])
    subs = _split_on_drift(0, len(midi), time, hz, dict(DEFAULTS))

    # 3 个稳定级 → 3 个子段，边界单调、覆盖全区间
    assert len(subs) == 3
    assert subs[0][0] == 0 and subs[-1][1] == len(midi)
    for i in range(1, len(subs)):
        assert subs[i - 1][1] <= subs[i][0]

    # 每段音高中位数回到对应稳定级（切分不能把不同级混成一锅）
    meds = [round(float(np.median([hz_to_midi(hz[i]) for i in range(a, b)])), 0) for a, b in subs]
    assert meds == [69.0, 71.0, 74.0], meds

    # 回归点：第二次切分边界不得吞掉 D5 起音（旧 bug 会把 B4 段拖进 D5）
    first_d5 = next(i for i, x in enumerate(midi) if x >= 73.5)
    assert subs[1][1] <= first_d5, f"第二段结束 {subs[1][1]} 越过了首个 D5 帧 {first_d5}"


def test_split_on_drift_single_level_no_split():
    # 单一稳定音高（无漂移）→ 整段返回，不误切
    midi = [69.0] * 40
    time = np.arange(len(midi)) * 0.01
    hz = np.array([midi_to_hz(x) for x in midi])
    subs = _split_on_drift(0, len(midi), time, hz, dict(DEFAULTS))
    assert subs == [(0, len(midi))]
