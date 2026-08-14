"""DSP 转录入口：音频 wav → Voice（ADR-0004 两层 schema 的原始层）。

M4 定案：转录层用注册表插件架构（`tsov/dsp/backends/`，照搬渲染层 backend.py）。
- game（openvpi/GAME 歌声→MIDI 专用模型）为唯一后端——GAME 输出音乐化音符边界 +
  浮点音高（deviation_cents 有值），优于 CREPE 的帧级音高 + 规则分割。
- CREPE/basic-pitch 方案已弃用删除（M3-FIX 的八度校正/漂移切分随 crepe 代码移除，
  不再需要规则补丁）。

接口不变：`transcribe(audio_path, backend, **params) -> Voice`，pipeline / CLI / eval 零改动。
输出统一 ADR-0005 Voice / Note schema（字段冻结，不许改）。
"""

from __future__ import annotations

from ..core.notes import Note, Voice
from .backends import get_backend, list_backends
from .segment import split_into_segments
from .tempo import estimate_bpm

# 兼容旧引用：动态后端名列表（M2 时是硬编码元组，现从注册表取）
BACKENDS = tuple(list_backends())


def transcribe(audio_path: str, backend: str = "game", **params) -> Voice:
    """音频 → 结构化声部对象（原始层）。

    - backend：注册表内任意转录后端名（当前：game）；默认 game
    - 输出：Note（start/end/pitch_midi/pitch_hz/velocity/confidence/deviation_cents/is_ornament）
      + bpm / bpm_confidence / segments / source_audio / backend
    - **params 透传后端（game：model / timeout / batch_size / workdir 等）
    """
    if backend not in BACKENDS:
        raise ValueError(f"未知 backend：{backend!r}（可选 {BACKENDS}）")
    return get_backend(backend).transcribe(audio_path, **params)


# ---------------------------------------------------------------------------
# Voice 组装（供各后端复用）
# ---------------------------------------------------------------------------

MIN_NOTE_S = 0.03  # 重叠裁剪后的残渣阈值：短于此删除（纯重叠残音）


def _fix_overlaps(notes: list[Note]) -> list[Note]:
    """音符重叠修复（M8）：单声部旋律不允许叠音。

    - 相邻音符 note[i].end > note[i+1].start → 裁剪 note[i].end = note[i+1].start（保后音 onset）
    - 裁剪后 end-start < MIN_NOTE_S 的微音删除（纯重叠残渣）
    对 GAME（成品歌 81% 重叠）和 RMVPE（哼唱个别重叠）都无害。
    """
    if len(notes) < 2:
        return notes
    out = sorted(notes, key=lambda n: n.start)
    for i in range(1, len(out)):
        if out[i - 1].end > out[i].start:
            out[i - 1].end = out[i].start
    return [n for n in out if (n.end - n.start) >= MIN_NOTE_S]


def _build_voice(audio_path: str, backend: str, notes: list[Note]) -> Voice:
    segments = split_into_segments(notes)  # M8：先用原始间隔分割（重叠裁剪会清零 gap，破坏乐句）
    notes = _fix_overlaps(notes)  # M8：重叠裁剪（保证落盘干净）
    bpm, bpm_conf = estimate_bpm(notes)  # M7：BPM 从硬编码 120 改为 IOI 估计
    return Voice(
        notes=notes,
        bpm=bpm,
        bpm_confidence=bpm_conf,
        segments=segments,
        source_audio=audio_path,
        backend=backend,
    )
