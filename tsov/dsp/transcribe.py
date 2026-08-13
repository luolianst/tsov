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

def _build_voice(audio_path: str, backend: str, notes: list[Note]) -> Voice:
    segments = split_into_segments(notes)
    return Voice(
        notes=notes,
        bpm=120.0,
        bpm_confidence=0.0,
        segments=segments,
        source_audio=audio_path,
        backend=backend,
    )
