"""tsov.core —— 表示层数据模型（六类全定，ADR-0005）。

schema 冻结：字段名 / 类型以 ADR-0005 类定义为基准，opencode 迭代不许改。
序列化辅助（to_dict / from_dict）不是 schema 的一部分，仅方便落盘镜像。
"""

from .notes import Note, Segment, Voice
from .score import Effect, Instrument, KeyCandidate, Score, Track

__all__ = [
    "Note",
    "Segment",
    "Voice",
    "Effect",
    "Instrument",
    "KeyCandidate",
    "Score",
    "Track",
]
