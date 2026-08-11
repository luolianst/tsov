"""核心数据模型：Note / Segment / Voice（ADR-0005 定义，schema 冻结）。

字段名与类型严格按 ADR-0005；追加的 to_dict / from_dict 仅为 JSON 落盘镜像服务。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Note:
    start: float  # 秒（绝对时间）
    end: float  # 秒
    pitch_midi: int  # 量化后 MIDI 音高
    pitch_hz: float  # 原始频率（DSP 直出）
    velocity: float = 0.8  # 0-1 力度
    confidence: float = 1.0  # 0-1 转录置信度
    deviation_cents: float = 0.0  # 相对量化网格的音分偏移（表现力保留）
    is_ornament: bool = False  # 装饰音标记（短音/倚音倾向）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Note":
        return cls(**data)


@dataclass
class Segment:
    start: float  # 秒
    end: float  # 秒
    type: str  # 乐句/段落类型（如 "phrase" / "pause" / "breath"）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Segment":
        return cls(**data)


@dataclass
class Voice:
    """一段哼唱 = 一个声部"""

    notes: list[Note]
    bpm: float = 120.0
    bpm_confidence: float = 0.0
    segments: list[Segment] = field(default_factory=list)  # 乐句/段落 {start, end, type}
    source_audio: str = ""  # 来源文件路径（溯源）
    backend: str = ""  # DSP 后端（crepe_notes / basic-pitch）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Voice":
        return cls(
            notes=[Note.from_dict(n) for n in data["notes"]],
            bpm=data["bpm"],
            bpm_confidence=data["bpm_confidence"],
            segments=[Segment.from_dict(s) for s in data["segments"]],
            source_audio=data["source_audio"],
            backend=data["backend"],
        )
