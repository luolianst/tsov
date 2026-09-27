"""导出矩阵（M-V4）：master / 总线 / 轨道 stems（WAV）+ 多轨 MIDI（+ 每轨 MIDI）。

矩阵维度：内容（master 混音 × 总线 stems × 轨道 stems）× 格式（WAV × MIDI）
- 统一缩放系数：先探测 full mix 峰值，>1 时对**全部产物**应用同一系数（相对电平保持）
- stems = 轨内处理（fader/pan/automation）后、总线/master 处理前；**不含 send 支路**（E5）
- buses = 含该总线处理（效果链 → fader/pan/automation）+ 汇入该总线的 send 支路、master 处理前（E5）
- mix = 全量（含全部 send 支路与总线/master 效果链）
- mute/solo 与 full mix 一致地作用于全部产物（solo 生效时非 solo 轨的 stem 为静音）
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from ..core.score import Score
from .device import write_wav
from .mix import render_buses
from .session import HostSession

_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")

_SUBTYPES = {"16": "PCM_16", "pcm_16": "PCM_16", "24": "PCM_24", "pcm_24": "PCM_24",
             "32": "FLOAT", "32f": "FLOAT", "float": "FLOAT"}


def _subtype_of(bit_depth) -> str:
    """位深 → WAV subtype（E6 段2：16/24/32f；默认 16=PCM_16）。"""
    sub = _SUBTYPES.get(str(bit_depth).strip().lower())
    if sub is None:
        raise ValueError(f"不支持的位深：{bit_depth!r}（可选 16 / 24 / 32f）")
    return sub


def _slice_range(audio, sr: int, range_):
    """选段切片（E6 段2）：[起, 止] 秒 → 样本区间（样本级取整；越界即报错）。"""
    a, b = float(range_[0]), float(range_[1])
    if not (a >= 0 and b > a):
        raise ValueError(f"选段非法：[{a}, {b}]（需 0 ≤ 起 < 止）")
    n = audio.shape[0]
    s0, s1 = int(round(a * sr)), int(round(b * sr))
    s0, s1 = max(0, min(s0, n)), max(0, min(s1, n))
    if s1 <= s0:
        raise ValueError(f"选段超出音频范围（音频 {n / sr:.3f}s，选段 [{a}, {b}]）")
    return audio[s0:s1]


def _slug(name: str, fallback: str) -> str:
    s = _SAFE.sub("-", str(name or "")).strip("-")
    return s or fallback


def export_matrix(
    session: HostSession,
    out_dir,
    *,
    samplerate: int | None = None,
    stereo: bool = True,
    mix: bool = True,
    buses: bool = True,
    stems: bool = True,
    midi: bool = True,
    midi_stems: bool = False,
    bit_depth=16,
    range=None,
    cache=None,
) -> dict:
    """导出矩阵 → 报告 dict（含全部产物路径清单 `files`）。cache：ADR-0018 stem 库（可选）。

    E6 段2：bit_depth 16/24/32f → WAV subtype；range=[起, 止] 秒 → **音频产物**切片导出（MIDI 维持全曲）。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sr = int(samplerate or session.samplerate)
    sub = _subtype_of(bit_depth)
    rng = None
    if range is not None:
        if not (isinstance(range, (list, tuple)) and len(range) == 2):
            raise ValueError("range 需为 [起, 止] 两元素（秒）")
        rng = [float(range[0]), float(range[1])]
        if not (rng[0] >= 0 and rng[1] > rng[0]):
            raise ValueError(f"选段非法：[{rng[0]}, {rng[1]}]（需 0 ≤ 起 < 止）")

    report: dict = {"out_dir": str(out), "samplerate": sr, "stereo": bool(stereo),
                    "bit_depth": sub, "files": []}
    if rng is not None:
        report["range"] = [round(rng[0], 6), round(rng[1], 6)]

    # 统一缩放系数（探针 = full mix；auto_scale=False 拿原始峰值）
    probe = render_buses(session, sr, stereo=stereo, auto_scale=False, cache=cache)
    peak = float(np.max(np.abs(probe))) if probe.size else 0.0
    scale = (1.0 / peak) if peak > 1.0 else 1.0
    report["scale"] = round(scale, 6)

    if rng is not None:
        _slice_range(probe, sr, rng)   # 选段先校验（越界立即报错，不白写产物）

    def _cut(buf):
        return buf if rng is None else _slice_range(buf, sr, rng)

    if mix:
        path = write_wav(_cut(probe * scale), out / "mix.wav", sr, subtype=sub)
        report["mix"] = path
        report["files"].append(path)

    if buses:
        names = sorted({(getattr(t.track, "bus", "master") or "master") for t in session.tracks} - {"master"})
        report["buses"] = {}
        for name in names:
            audio = render_buses(
                session, sr, stereo=stereo, only_bus=name,
                include_master_processing=False, auto_scale=False, cache=cache,
            )
            path = write_wav(_cut(audio * scale), out / "buses" / f"{_slug(name, 'bus')}.wav", sr, subtype=sub)
            report["buses"][name] = path
            report["files"].append(path)

    if stems:
        report["stems"] = {}
        for i, ht in enumerate(session.tracks):
            audio = render_buses(
                session, sr, stereo=stereo, only_track=i,
                include_bus_processing=False, include_master_processing=False, auto_scale=False, cache=cache,
            )
            path = write_wav(_cut(audio * scale), out / "stems" / f"{i:02d}-{_slug(ht.track.name, 'track')}.wav", sr, subtype=sub)
            report["stems"][ht.track.name or f"track-{i}"] = path
            report["files"].append(path)

    if midi:
        from ..midi.export import score_to_midi

        path = score_to_midi(session.score, str(out / "score.mid"))
        report["midi"] = path
        report["files"].append(path)

    if midi_stems:
        from ..midi.export import score_to_midi

        (out / "midi").mkdir(parents=True, exist_ok=True)
        report["midi_stems"] = {}
        for i, ht in enumerate(session.tracks):
            single = Score(
                title=session.score.title,
                tempo=session.score.tempo,
                key_candidates=list(session.score.key_candidates),
                tracks=[ht.track],
                meta=dict(session.score.meta),
            )
            path = score_to_midi(single, str(out / "midi" / f"{i:02d}-{_slug(ht.track.name, 'track')}.mid"))
            report["midi_stems"][ht.track.name or f"track-{i}"] = path
            report["files"].append(path)

    return report
