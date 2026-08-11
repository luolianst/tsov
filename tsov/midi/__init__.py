"""tsov.midi —— MIDI 导入/导出（Score ↔ MIDI，pretty_midi）。

M3 填充实现（哼唱 → MIDI → fluidsynth 回放 WAV）。
"""

from .export import score_to_midi

__all__ = ["score_to_midi"]
