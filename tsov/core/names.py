"""音名工具：MIDI 音高 ↔ 音名（纯函数）。

2026-09-22 架构治理（F1）：自 tsov/analysis/dataset.py 下沉——纯工具归 core 底部，
消除 host → analysis 反向依赖（dataset 的语义层拼装留在原处）。
2026-09-26 E4 段3：补 note_name_to_midi（配器模式库 register 解析用；与
agent/tools_compose 的解析语义一致——那里是历史副本）。
"""

from __future__ import annotations

import re

# 音名表：MIDI 音高 → 音名（midi % 12），八度 = midi // 12 - 1（midi 60 = C4）
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

_NOTE_BASE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def midi_to_note_name(pitch_midi: int) -> str:
    """MIDI 音高 → 音名 + 八度（如 60 → C4，69 → A4）。"""
    pitch_midi = int(pitch_midi)
    name = NOTE_NAMES[pitch_midi % 12]
    octave = pitch_midi // 12 - 1
    return f"{name}{octave}"


def note_name_to_midi(name: str) -> int | None:
    """音名 → MIDI 音高（'E4' / 'f#3' / 'Bb2'；C4=60）；解析失败 → None。"""
    s = str(name).strip().lower().replace("♯", "#").replace("♭", "b")
    m = re.fullmatch(r"([a-g])([#b]?)(-?\d+)", s)
    if not m:
        return None
    semi = _NOTE_BASE[m.group(1)] + (1 if m.group(2) == "#" else -1 if m.group(2) == "b" else 0)
    return (int(m.group(3)) + 1) * 12 + semi
