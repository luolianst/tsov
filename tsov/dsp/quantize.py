"""音符量化规则（DSP 原始 → 量化网格）。M2 实现（ADR-0005 字段冻结）。

- 量化后 pitch_midi 固定；原始频率保留在 pitch_hz
- 相对量化网格的音分偏移写入 deviation_cents（表现力保留，不丢掉）
- 时值量化：start/end 对齐到 1/n 拍网格（默认 16 分音符）
"""

from __future__ import annotations

from dataclasses import replace

from ..core.notes import Note
from ..core.units import hz_to_deviation_cents


def quantize_notes(notes: list[Note], bpm: float = 120.0, grid: int = 16, **params) -> list[Note]:
    """规则量化：把 DSP 直出的原始音符对齐到节拍网格。

    - grid：每拍细分数（16 = 16 分音符网格；8 = 八分音符）
    - 音高：pitch_midi 保持不变（原始层已量化到半音）；deviation_cents 按 pitch_hz 重算
    - 时值：start/end 取整到最近网格点；end<=start 时补一个网格单元
    """
    beat_s = 60.0 / bpm
    cell_s = beat_s / grid
    out: list[Note] = []
    for n in notes:
        start = round(n.start / cell_s) * cell_s
        end = round(n.end / cell_s) * cell_s
        if end <= start:
            end = start + cell_s
        out.append(
            replace(
                n,
                start=round(start, 6),
                end=round(end, 6),
                deviation_cents=round(hz_to_deviation_cents(n.pitch_hz), 3),
            )
        )
    return out
