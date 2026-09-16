"""核心数据模型：Effect / Instrument / Track / Score / KeyCandidate（ADR-0005，schema 冻结）。

字段名与类型严格按 ADR-0005；追加的 to_dict / from_dict 仅为 JSON 落盘镜像服务。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .notes import Note


def parse_time_signature(sig: str | None) -> tuple[int, int]:
    """'6/8' → (6, 8)；非法/缺省 → (4, 4)（M-V6：Score.time_signature 的统一解析口径，MIDI 导出/工具共用）。"""
    try:
        num_s, den_s = str(sig or "").split("/", 1)
        num, den = int(num_s), int(den_s)
    except (ValueError, AttributeError):
        return 4, 4
    if not (1 <= num <= 16) or den not in (1, 2, 4, 8, 16):
        return 4, 4
    return num, den


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
    # ---- M-V4 混音字段（ADR-0005 向后兼容增补；缺省值与旧数据行为一致）----
    bus: str = "master"          # 汇入哪条总线（Score.buses 的 name；未知/缺省 = master）
    pan: float = 0.0             # -1 全左 .. +1 全右（线性平衡律：对侧衰减）
    mute: bool = False
    solo: bool = False
    automation: dict = field(default_factory=dict)  # {"volume": [[t, 倍率], ...], "pan": [[t, -1..1], ...]}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(
            name=data["name"],
            instrument=Instrument.from_dict(data["instrument"]),
            notes=[Note.from_dict(n) for n in data["notes"]],
            bus=data.get("bus", "master") or "master",
            pan=float(data["pan"]) if data.get("pan") is not None else 0.0,
            mute=bool(data.get("mute", False)),
            solo=bool(data.get("solo", False)),
            automation=dict(data.get("automation") or {}),
        )


@dataclass
class Bus:
    """混音总线（M-V4）：若干 track 的汇聚点，经总线处理（volume/pan/automation）后进 master。"""

    name: str = "master"
    volume: float = 1.0
    pan: float = 0.0
    automation: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Bus":
        return cls(
            name=data.get("name", "master") or "master",
            volume=float(data["volume"]) if data.get("volume") is not None else 1.0,
            pan=float(data["pan"]) if data.get("pan") is not None else 0.0,
            automation=dict(data.get("automation") or {}),
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
    time_signature: str = "4/4"  # 拍号（M-V6 增补，缺省 4/4；形如 "6/8"/"3/4"，向后兼容）
    key_candidates: list[KeyCandidate] = field(default_factory=list)  # 调性候选分布 {key, confidence}
    tracks: list[Track] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  # 生成元信息（run-id / 时间 / 来源）
    # ---- M-V4 混音字段（向后兼容增补）----
    buses: list[Bus] = field(default_factory=list)  # 附加总线（master 隐式存在，不列在这里）
    master: Bus = field(default_factory=lambda: Bus(name="master"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Score":
        return cls(
            title=data["title"],
            tempo=data["tempo"],
            time_signature=data.get("time_signature") or "4/4",
            key_candidates=[KeyCandidate.from_dict(k) for k in data["key_candidates"]],
            tracks=[Track.from_dict(t) for t in data["tracks"]],
            meta=data["meta"],
            buses=[Bus.from_dict(b) for b in data.get("buses") or []],
            master=Bus.from_dict(data["master"]) if data.get("master") else Bus(name="master"),
        )
