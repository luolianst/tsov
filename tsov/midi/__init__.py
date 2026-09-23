"""tsov.midi —— MIDI 导入/导出（Score ↔ MIDI，pretty_midi）。

M3 填充实现（哼唱 → MIDI → fluidsynth 回放 WAV）。
"""

from .export import score_to_midi

# midi_to_score（tsov/midi/import.py）：模块名是关键字，常规 import 不可达——经 importlib 访问
__all__ = ["score_to_midi"]
