"""音名工具：MIDI 音高 → 音名 + 八度（纯函数）。

2026-09-22 架构治理（F1）：自 tsov/analysis/dataset.py 下沉——纯工具归 core 底部，
消除 host → analysis 反向依赖（dataset 的语义层拼装留在原处）。
"""

from __future__ import annotations

# 音名表：MIDI 音高 → 音名（midi % 12），八度 = midi // 12 - 1（midi 60 = C4）
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_to_note_name(pitch_midi: int) -> str:
    """MIDI 音高 → 音名 + 八度（如 60 → C4，69 → A4）。"""
    pitch_midi = int(pitch_midi)
    name = NOTE_NAMES[pitch_midi % 12]
    octave = pitch_midi // 12 - 1
    return f"{name}{octave}"
