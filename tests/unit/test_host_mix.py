"""M-V4 总线化混音测试：兼容性 / 总线路由 / pan 平衡律 / mute+solo / automation。

用假音源（常数信号）保证数值可手算；低采样率加速。
"""

from __future__ import annotations

import numpy as np

from tsov.core.notes import Note
from tsov.core.score import Bus, Instrument, Score, Track
from tsov.host import HostSession, HostTrack, mix_graph, render_buses
from tsov.host.instrument import SoundSource

SR = 8000


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
        )
        score_tracks.append(tr)
        host_tracks.append(HostTrack(track=tr, source=_ConstSource(s.get("value", 0.0))))
    score = Score(
        title="t", tempo=100.0, tracks=score_tracks,
        buses=buses or [], master=master or Bus(name="master"),
    )
    return HostSession(score, host_tracks, samplerate=SR)


def _mid(audio):
    """取中段样本（避开首尾边界）。"""
    return float(audio[len(audio) // 2])


def test_default_mix_backward_compat():
    """缺省数据（无 pan/mute/auto）下 mono 混音 == 旧公式（Σ render*volume）。"""
    s = _session([{"value": 0.2, "volume": 1.0}, {"value": 0.3, "volume": 0.5}])
    mono = mix_graph(s)
    assert abs(_mid(mono) - (0.2 + 0.3 * 0.5)) < 1e-6
    stereo_mix = render_buses(s, stereo=False)
    assert np.allclose(mono, stereo_mix, atol=1e-6)


def test_bus_routing_and_bus_volume():
    """track → bus 路由 + 总线音量 + master 音量。"""
    s = _session(
        [{"value": 0.4, "name": "a"}, {"value": 0.4, "name": "b", "bus": "b1"}],
        buses=[Bus(name="b1", volume=0.5)],
        master=Bus(name="master", volume=0.5),
    )
    mono = mix_graph(s)
    # a: 0.4 → master；b: 0.4*0.5(总线)=0.2 → master；(0.4+0.2)*0.5 = 0.3
    assert abs(_mid(mono) - 0.3) < 1e-6


def test_pan_balance_law_and_mono_fold():
    """pan 平衡律：中位不衰减、对侧衰减；mono 折叠 (L+R)/2。"""
    s_c = _session([{"value": 0.5, "pan": 0.0}])
    st = render_buses(s_c, stereo=True)
    assert abs(st[len(st) // 2, 0] - 0.5) < 1e-6 and abs(st[len(st) // 2, 1] - 0.5) < 1e-6
    assert abs(_mid(render_buses(s_c, stereo=False)) - 0.5) < 1e-6

    s_l = _session([{"value": 0.5, "pan": -1.0}])
    stl = render_buses(s_l, stereo=True)
    assert abs(stl[len(stl) // 2, 0] - 0.5) < 1e-6 and abs(stl[len(stl) // 2, 1]) < 1e-6
    assert abs(_mid(render_buses(s_l, stereo=False)) - 0.25) < 1e-6


def test_mute_and_solo():
    s = _session([{"value": 0.5, "name": "a"}, {"value": 0.5, "name": "b", "mute": True}])
    assert abs(_mid(mix_graph(s)) - 0.5) < 1e-6          # mute b
    s2 = _session([{"value": 0.5, "name": "a"}, {"value": 0.6, "name": "b", "solo": True}])
    assert abs(_mid(mix_graph(s2)) - 0.6) < 1e-6         # 仅 b 可闻


def test_zero_volume_not_overridden():
    """0.0 音量不允许被缺省值顶掉（回归：`x or 1.0` 类错误）。"""
    assert abs(_mid(mix_graph(_session([{"value": 0.5, "volume": 0.0}])))) < 1e-9
    s = _session([{"value": 0.5, "bus": "b1"}], buses=[Bus(name="b1", volume=0.0)])
    assert abs(_mid(mix_graph(s))) < 1e-9
    s2 = _session([{"value": 0.5}], master=Bus(name="master", volume=0.0))
    assert abs(_mid(mix_graph(s2))) < 1e-9


def test_automation_volume_ramp():
    """automation 音量斜坡 [[0,0],[2,0.5]]：t=1s → 0.5 倍率 → 0.8*0.5*0.5=0.2。"""
    s = _session([{"value": 0.8, "end": 2.0, "automation": {"volume": [[0, 0.0], [2, 0.5]]}}])
    mono = mix_graph(s)
    assert abs(float(mono[0]) - 0.0) < 1e-6
    assert abs(float(mono[SR * 1]) - 0.2) < 1e-5
    assert abs(float(mono[SR * 2]) - 0.4) < 1e-5


def test_automation_pan_move():
    """automation pan 从左扫到右：t=0 全左、t=2 全右。"""
    s = _session([{"value": 0.5, "end": 2.0, "automation": {"pan": [[0, -1.0], [2, 1.0]]}}])
    st = render_buses(s, stereo=True)
    assert abs(float(st[0, 0]) - 0.5) < 1e-6 and abs(float(st[0, 1])) < 1e-6
    assert abs(float(st[SR * 2, 1]) - 0.5) < 1e-6 and abs(float(st[SR * 2, 0])) < 1e-6
