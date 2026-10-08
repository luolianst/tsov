"""v0.2 批C2 段3 单测：Score.folders（文件夹层 v1）——op 校验 / 求值 / 缺省同构。

覆盖面（设计件 §3.4、任务书 F6）：
- set_folder_mix：创建/更新/volume 钳制/参数校验
- folders 序列化同构（无字段 → {}，往返保留）
- 求值：音量乘子 / automation 曲线 / 乘性级联 / mute / solo（与轨 solo 交互规则锁死）
- 缺省同构：轨道带 folder 归属但 folders 无条目 → 零行为变化（逐位）
"""

from __future__ import annotations

import numpy as np

from tsov.core.notes import Note
from tsov.core.score import Instrument, Score, Track
from tsov.host import EditBatch, HostSession, HostTrack, render_buses
from tsov.host.instrument import SoundSource

SR = 8000


class _ConstSource(SoundSource):
    """常数信号源（忽略 notes，方便手算）。"""

    def __init__(self, value: float):
        self.value = float(value)

    def render(self, notes, samplerate, n_frames):
        return np.full((n_frames,), self.value, dtype=np.float32)


def _session(specs, folders=None):
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
            folder=s.get("folder", ""),
        )
        score_tracks.append(tr)
        host_tracks.append(HostTrack(track=tr, source=_ConstSource(s.get("value", 0.0))))
    score = Score(title="t", tempo=100.0, tracks=score_tracks, folders=folders or {})
    return HostSession(score, host_tracks, samplerate=SR)


def _mid(audio):
    return float(audio[len(audio) // 2])


def _score():
    return Score(title="t", tempo=120.0, tracks=[Track(name="a")])


# ---------------- op 校验 ----------------


def test_set_folder_mix_create_and_update():
    out, res = EditBatch().add("set_folder_mix", value={"folder": "A"}).apply(_score())
    assert res.ok and res.applied == 1
    assert out.folders == {"A": {"volume": 1.0, "mute": False, "solo": False, "automation": {}}}
    out2, res2 = EditBatch().add("set_folder_mix", value={"folder": "A", "volume": 0.5, "mute": True}).apply(out)
    assert res2.ok and out2.folders["A"]["volume"] == 0.5 and out2.folders["A"]["mute"] is True
    assert out.folders["A"]["volume"] == 1.0  # 原 Score 不被修改（深拷贝事务）


def test_set_folder_mix_clamp_and_reject():
    out, res = EditBatch().add("set_folder_mix", value={"folder": "A", "volume": 9}).apply(_score())
    assert res.ok and out.folders["A"]["volume"] == 2.0
    out2, _ = EditBatch().add("set_folder_mix", value={"folder": "A", "volume": -3}).apply(_score())
    assert out2.folders["A"]["volume"] == 0.0
    _o, r3 = EditBatch().add("set_folder_mix", value={}).apply(_score())
    assert r3.applied == 0 and "folder" in r3.errors[0]
    _o4, r4 = EditBatch().add("set_folder_mix", value={"folder": "A", "volume": "x"}).apply(_score())
    assert r4.applied == 0


def test_folders_roundtrip_old_isomorphic():
    s = Score.from_dict({"title": "t", "tempo": 120.0, "key_candidates": [], "tracks": [], "meta": {}})
    assert s.folders == {}  # 旧数据无字段 → 缺省空（同构）
    s2 = Score.from_dict({"title": "t", "tempo": 120.0, "key_candidates": [], "tracks": [], "meta": {},
                          "folders": {"A": {"volume": 0.5, "mute": True, "solo": False, "automation": {}}}})
    assert s2.folders["A"]["volume"] == 0.5 and s2.folders["A"]["mute"] is True
    assert s2.to_dict()["folders"]["A"]["solo"] is False


# ---------------- 求值 ----------------


def test_folder_volume_multiplier():
    base = _session([{"value": 0.8, "end": 2.0}])
    s = _session([{"value": 0.8, "end": 2.0, "folder": "A"}], folders={"A": {"volume": 0.5}})
    assert abs(_mid(render_buses(base, stereo=False)) - 0.8) < 1e-6
    assert abs(_mid(render_buses(s, stereo=False)) - 0.4) < 1e-6


def test_folder_curve_multiplies():
    s = _session([{"value": 0.8, "end": 2.0, "folder": "A"}],
                 folders={"A": {"volume": 1.0, "automation": {"volume": [[0, 0.0], [2, 0.5]]}}})
    mono = render_buses(s, stereo=False)
    assert abs(float(mono[0])) < 1e-6
    assert abs(float(mono[SR * 1]) - 0.8 * 0.25) < 1e-5  # t=1 → 曲线 0.25×


def test_folder_curve_and_volume_cascade():
    """乘性级联：底值 × 文件夹 volume × 文件夹曲线。"""
    s = _session([{"value": 0.8, "end": 2.0, "folder": "A"}],
                 folders={"A": {"volume": 0.5, "automation": {"volume": [[0, 1.0], [2, 0.0]]}}})
    mono = render_buses(s, stereo=False)
    assert abs(float(mono[0]) - 0.4) < 1e-6            # 0.8×0.5×1.0
    assert abs(float(mono[SR * 2])) < 1e-5             # 0.8×0.5×0.0


def test_folder_mute_and_solo():
    s = _session([{"value": 0.5, "name": "a", "folder": "A"}, {"value": 0.5, "name": "b"}],
                 folders={"A": {"mute": True}})
    assert abs(_mid(render_buses(s, stereo=False)) - 0.5) < 1e-6      # a 静音 → 仅 b
    s2 = _session([{"value": 0.5, "name": "a", "folder": "A"}, {"value": 0.5, "name": "b"}],
                  folders={"A": {"solo": True}})
    assert abs(_mid(render_buses(s2, stereo=False)) - 0.5) < 1e-6     # 仅 a 可闻

    # 与轨 solo 交互（规则锁死）：b solo → a（无文件夹 solo）不可闻；a 文件夹 solo → 并闻
    s3 = _session([{"value": 0.4, "name": "a", "folder": "A"}, {"value": 0.6, "name": "b", "solo": True}])
    assert abs(_mid(render_buses(s3, stereo=False)) - 0.6) < 1e-6
    s4 = _session([{"value": 0.4, "name": "a", "folder": "A"}, {"value": 0.6, "name": "b", "solo": True}],
                  folders={"A": {"solo": True}})
    assert abs(_mid(render_buses(s4, stereo=False)) - 1.0) < 1e-5


def test_folder_attr_without_entry_zero_change():
    """轨道带 folder 归属但 folders 无条目 → 零行为变化（缺省同构）。"""
    s = _session([{"value": 0.7, "end": 2.0, "folder": "A"}])
    assert abs(_mid(render_buses(s, stereo=False)) - 0.7) < 1e-6


def test_no_folders_bitwise_same():
    """无 folders 字段 vs 空 {} → 渲染逐位一致。"""
    a = render_buses(_session([{"value": 0.3, "end": 1.0}]), stereo=False)
    b = render_buses(_session([{"value": 0.3, "end": 1.0}], folders={}), stereo=False)
    assert np.array_equal(a, b)
