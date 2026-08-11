"""核心数据模型：Effect / Instrument / Track / Score / KeyCandidate（ADR-0005，schema 冻结）。

字段名与类型严格按 ADR-0005；追加的 to_dict / from_dict 仅为 JSON 落盘镜像服务。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .notes import Note


@dataclass
class Effect:
    type: str  # "reverb" / "compressor" / ...
    params: dict = field(default_factory=dict)  # 参数表

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Effect":
        return cls(**data)


@dataclass
class Instrument:
    backend: str = "fluidsynth"  # "fluidsynth" / "kontakt" / ...
    program: str = ""  # 音色/预设标识
    volume: float = 1.0
    effects: list[Effect] = field(default_factory=list)  # 效果器链（第一版可为空）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Instrument":
        return cls(
            backend=data["backend"],
            program=data["program"],
            volume=data["volume"],
            effects=[Effect.from_dict(e) for e in data["effects"]],
        )


@dataclass
class Track:
    name: str = ""
    instrument: Instrument = field(default_factory=Instrument)
    notes: list[Note] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(
            name=data["name"],
            instrument=Instrument.from_dict(data["instrument"]),
            notes=[Note.from_dict(n) for n in data["notes"]],
        )


@dataclass
class KeyCandidate:
    key: str  # 如 "C major" / "a minor"
    confidence: float  # 0-1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "KeyCandidate":
        return cls(**data)


@dataclass
class Score:
    title: str = ""
    tempo: float = 120.0  # BPM
    key_candidates: list[KeyCandidate] = field(default_factory=list)  # 调性候选分布 {key, confidence}
    tracks: list[Track] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # 生成元信息（run-id / 时间 / 来源）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Score":
        return cls(
            title=data["title"],
            tempo=data["tempo"],
            key_candidates=[KeyCandidate.from_dict(k) for k in data["key_candidates"]],
            tracks=[Track.from_dict(t) for t in data["tracks"]],
            meta=data["meta"],
        )
