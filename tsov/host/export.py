"""导出矩阵（M-V4）：master / 总线 / 轨道 stems（WAV）+ 多轨 MIDI（+ 每轨 MIDI）。

矩阵维度：内容（master 混音 × 总线 stems × 轨道 stems）× 格式（WAV × MIDI）
- 统一缩放系数：先探测 full mix 峰值，>1 时对**全部产物**应用同一系数（相对电平保持）
- stems = 轨内处理（fader/pan/automation）后、总线/master 处理前
- buses = 含该总线处理（fader/pan/automation）、master 处理前
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
    cache=None,
) -> dict:
    """导出矩阵 → 报告 dict（含全部产物路径清单 `files`）。cache：ADR-0018 stem 库（可选）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sr = int(samplerate or session.samplerate)

    report: dict = {"out_dir": str(out), "samplerate": sr, "stereo": bool(stereo), "files": []}

    # 统一缩放系数（探针 = full mix；auto_scale=False 拿原始峰值）
    probe = render_buses(session, sr, stereo=stereo, auto_scale=False, cache=cache)
    peak = float(np.max(np.abs(probe))) if probe.size else 0.0
    scale = (1.0 / peak) if peak > 1.0 else 1.0
    report["scale"] = round(scale, 6)

    if mix:
        path = write_wav(probe * scale, out / "mix.wav", sr)
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
            path = write_wav(audio * scale, out / "buses" / f"{_slug(name, 'bus')}.wav", sr)
            report["buses"][name] = path
            report["files"].append(path)

    if stems:
        report["stems"] = {}
        for i, ht in enumerate(session.tracks):
            audio = render_buses(
                session, sr, stereo=stereo, only_track=i,
                include_bus_processing=False, include_master_processing=False, auto_scale=False, cache=cache,
            )
            path = write_wav(audio * scale, out / "stems" / f"{i:02d}-{_slug(ht.track.name, 'track')}.wav", sr)
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


def export_score_file(score_path, out_dir, *, soundfont: str | None = None, samplerate: int = 44100, **opts) -> dict:
    """Score JSON → 导出矩阵（一步式，CLI 用）。"""
    import json

    from .engine import HostEngine

    score = Score.from_dict(json.loads(Path(score_path).read_text(encoding="utf-8")))
    engine = HostEngine(soundfont=soundfont, samplerate=samplerate)
    session = engine.load(score)
    try:
        return export_matrix(session, out_dir, samplerate=samplerate, **opts)
    finally:
        session.close()
