"""DSP 后端适配器（M2 双验证：crepe_notes vs basic-pitch）。

统一输出 ADR-0005 的 Voice 原始层 schema；M2 填充实现后进对比测试。
"""

from ..core.notes import Voice

__all__ = ["transcribe_crepe_notes", "transcribe_basic_pitch"]


def transcribe_crepe_notes(audio_path: str, **params) -> Voice:
    """CREPE f0 → CREPE Notes 音符分割 → Voice（xavriley/crepe_notes）。"""
    raise NotImplementedError("M2 填充：crepe_notes 后端")


def transcribe_basic_pitch(audio_path: str, **params) -> Voice:
    """spotify/basic-pitch 音频→音符 → Voice（转成统一 schema）。"""
    raise NotImplementedError("M2 填充：basic-pitch 后端")
