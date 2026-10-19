"""P83 显式 freeze：内容指纹 / 单轨 BK1 渲染 / 资产与 stale 校验。

冻结口径（grill 2026-10-19）：
- 轨级整轨；音源 + 该轨插入效果（效果后、推子前）；不含音量/声像/automation/总线。
- 资产复用 StemStore（float32 WAV、内容寻址、track_key 为键）。
- 从时间轴 0 到「轨内容末尾 + 1s 释放 + 效果 tail」；0 起点便于 sidecar 直接播放。
- 资产缺失/指纹不匹配 = stale，播放/导出显式失败，不自动解冻。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..core.score import Score, Track
from .cache import StemStore, track_key
from .effect import effect_tail_seconds
from .mix import render_track_source
from .session import HostSession

FREEZE_VERSION = 1


class FreezeError(RuntimeError):
    """冻结前置不满足（空轨/VST2/渲染失败等）。"""


def track_content_end(track: Track) -> float:
    return max((float(n.end) for n in track.notes), default=0.0)


def freeze_fingerprint(track: Track, score: Score, samplerate: int) -> str:
    """冻结内容指纹：track_key（音源/参数/效果/音符/渲染代码）+ tempo/拍号。"""
    payload = {
        "v": FREEZE_VERSION,
        "sr": int(samplerate),
        "track": track_key(track, int(samplerate)),
        "tempo": round(float(score.tempo), 6),
        "time_signature": str(score.time_signature),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:24]


def _validate_track(track: Track) -> None:
    if str(getattr(track, "kind", "midi") or "midi") != "midi":
        raise FreezeError("只能冻结 MIDI/插件轨（音频轨不支持）")
    if not track.notes:
        raise FreezeError("空轨不能冻结（没有可渲染内容）")
    program = str(getattr(track.instrument, "program", "") or "")
    if program.startswith("vst2:") or program.lower().endswith(".dll"):
        raise FreezeError("VST2 轨不支持冻结（可离线导出）")


def render_freeze_asset(
    project,
    track_index: int,
    *,
    samplerate: int = 44100,
    store: StemStore | None = None,
) -> dict:
    """渲染一条轨的 BK1 输出并写入 StemStore；返回 freeze metadata。

    调用方负责先停止 live 播放；本函数只做离线渲染/落资产，不写 score。
    """
    score: Score = project.score
    if not (0 <= int(track_index) < len(score.tracks)):
        raise FreezeError(f"轨道索引越界：{track_index}")
    track = score.tracks[int(track_index)]
    _validate_track(track)

    content_end = track_content_end(track)
    if content_end <= 0.0:
        raise FreezeError("空轨不能冻结（没有可渲染内容）")
    tail = 1.0 + max(0.0, float(effect_tail_seconds(getattr(track.instrument, "effects", None))))
    n_frames = max(1, int(round((content_end + tail) * int(samplerate))))

    # 只实例化目标轨：避免为冻结一条 MIDI 轨而被其它音频轨/重插件拖住；
    # bus 混音不在 BK1 边界内，因此这里用单轨临时 Score 渲染即可。
    target_score = Score(
        title=score.title,
        tempo=score.tempo,
        time_signature=score.time_signature,
        tracks=[track],
        buses=score.buses,
        master=score.master,
    )
    session = HostSession.from_score(
        target_score,
        samplerate=int(samplerate),
        base_dir=Path(project.root),
        lazy=True,
    )
    try:
        audio = render_track_source(session.tracks[0], int(samplerate), n_frames)
    finally:
        session.close()

    audio = np.ascontiguousarray(np.asarray(audio, dtype=np.float32))
    if audio.ndim == 1:
        audio = np.stack([audio, audio], axis=1)
    if audio.shape[0] != n_frames:
        # render_track_source 正常返回 n_frames；防御性补齐/裁剪，避免资产长度与元数据不一致。
        if audio.shape[0] < n_frames:
            audio = np.pad(audio, ((0, n_frames - audio.shape[0]), (0, 0)))
        else:
            audio = audio[:n_frames]

    store = store or StemStore(project.root)
    asset_id = track_key(track, int(samplerate))
    store.save(asset_id, audio, int(samplerate))
    fingerprint = freeze_fingerprint(track, score, int(samplerate))
    return {
        "asset_id": asset_id,
        "content_hash": fingerprint,
        "sample_rate": int(samplerate),
        "length": round(float(audio.shape[0]) / float(samplerate), 6),
        "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "track": int(track_index),
        "name": track.name,
    }


def freeze_state(track: Track, score: Score, store: StemStore, samplerate: int) -> dict:
    """返回 {state: unfrozen|frozen|stale|missing, ...}，供播放/导出/UI 共用。"""
    f = getattr(track, "freeze", None)
    if not f:
        return {"state": "unfrozen"}
    asset_id = str(f.get("asset_id") or "")
    expected = freeze_fingerprint(track, score, int(samplerate))
    if not asset_id or not store.has(asset_id):
        return {"state": "missing", "asset_id": asset_id, "expected": expected}
    if str(f.get("content_hash") or "") != expected:
        return {"state": "stale", "asset_id": asset_id, "expected": expected}
    if int(f.get("sample_rate") or 0) != int(samplerate):
        return {"state": "stale", "asset_id": asset_id, "expected": expected}
    return {"state": "frozen", "asset_id": asset_id, "expected": expected}
