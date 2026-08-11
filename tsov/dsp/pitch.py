"""音高换算工具：Hz ↔ MIDI ↔ 半音偏移（DSP / eval 共用）。

- hz_to_midi / midi_to_hz：十二平均律换算
- hz_to_deviation_cents：某频率相对最近 MIDI 音的偏差（音分）
"""

from __future__ import annotations

import numpy as np


def hz_to_midi(hz: float) -> float:
    """频率 → MIDI 音高（连续值，A4=440Hz=69）。"""
    if hz <= 0:
        return 0.0
    return 69.0 + 12.0 * np.log2(hz / 440.0)


def midi_to_hz(midi: float) -> float:
    """MIDI 音高（连续值）→ 频率（Hz）。"""
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def hz_to_deviation_cents(hz: float) -> float:
    """频率相对最近十二平均律 MIDI 音高的音分偏移（带符号）。

    - 正值 = 偏高；负值 = 偏低；0 = 恰好落在某音上
    - 用于 Note.deviation_cents（表现力保留：原始频率相对量化网格的偏移）
    """
    midi = hz_to_midi(hz)
    nearest = float(round(midi))
    return 1200.0 * np.log2(hz / midi_to_hz(nearest)) if hz > 0 else 0.0
