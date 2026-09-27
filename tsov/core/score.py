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


def _audio_clip_id(file: str, start: float, src_offset: float) -> str:
    """旧形态（{file, offset}）迁移时派生确定性 clip id（同输入 → 同 id，跨加载稳定）。"""
    import hashlib

    blob = f"{file}|{start:.6f}|{src_offset:.6f}".encode("utf-8")
    return "c" + hashlib.sha1(blob).hexdigest()[:7]


def normalize_audio_clips(audio: dict | None) -> list[dict]:
    """Track.audio → 规范化 clip 列表（M-V8 E6 音频编辑刀）。

    - 新形态 ``{"clips": [...]}``：逐条补齐缺省字段（数值化；clip_id 缺失时按内容派生）。
    - 旧形态 ``{"file": …, "offset": …}``（E2 第一刀单 clip/轨）：迁移为单 clip
      ``{src_offset: 0, src_len: None, stretch: 1, fade_in: 0, fade_out: 0}``。
    - ``src_len=None`` 语义 =「到文件尾」——core 层不读文件，实际时长由渲染层按文件截断。
    - 空/未知 → []。
    """
    a = dict(audio or {})
    raw = a.get("clips")
    out: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            src_len = item.get("src_len")
            clip = {
                "clip_id": str(item.get("clip_id") or ""),
                "file": str(item.get("file") or ""),
                "start": float(item.get("start") or 0.0),
                "src_offset": float(item.get("src_offset") or 0.0),
                "src_len": None if src_len is None else float(src_len),
                "stretch": float(item["stretch"]) if item.get("stretch") is not None else 1.0,
                "fade_in": float(item.get("fade_in") or 0.0),
                "fade_out": float(item.get("fade_out") or 0.0),
            }
            if not clip["clip_id"]:
                clip["clip_id"] = _audio_clip_id(clip["file"], clip["start"], clip["src_offset"])
            out.append(clip)
        return out
    rel = str(a.get("file") or "")
    if rel:
        start = float(a.get("offset") or 0.0)
        out.append(
            {
                "clip_id": _audio_clip_id(rel, start, 0.0),
                "file": rel,
                "start": start,
                "src_offset": 0.0,
                "src_len": None,
                "stretch": 1.0,
                "fade_in": 0.0,
                "fade_out": 0.0,
            }
        )
    return out


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
    # ---- M-V8 E1：组织层文件夹归属（单层，向后兼容增补；与总线「混音路由」解耦）----
    folder: str = ""             # 空 = 不在任何文件夹
    # ---- M-V8 E2：音频轨（additive；kind="midi" 时与旧数据完全同构）----
    kind: str = "midi"           # "midi" | "audio"
    audio: dict = field(default_factory=dict)  # 音频轨元数据：{"file": "audio/xxx.flac", "offset": 0.0}（工程内相对路径；第一刀单 clip/轨）
    # ---- M-V8 E5：Send 支路（additive；post-fader × 量 → 汇入目标总线缓冲）----
    sends: dict = field(default_factory=dict)  # {总线名: 量 0~1}；缺省空 dict = 无支路（旧数据完全同构）

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
            folder=str(data.get("folder") or ""),
            kind=str(data.get("kind") or "midi"),
            audio=dict(data.get("audio") or {}),
            sends={str(k): float(v) for k, v in (data.get("sends") or {}).items()},
        )

    def audio_clips(self) -> list[dict]:
        """规范化音频 clip 列表（M-V8 E6；兼容第一刀 {file, offset} 单字段形态）。"""
        return normalize_audio_clips(self.audio)


@dataclass
class Bus:
    """混音总线（M-V4）：若干 track 的汇聚点，经总线处理（volume/pan/automation）后进 master。"""

    name: str = "master"
    volume: float = 1.0
    pan: float = 0.0
    automation: dict = field(default_factory=dict)
    # ---- M-V8 E5：总线效果链（additive；处理序 效果 → 音量/automation → 声像）----
    effects: list[Effect] = field(default_factory=list)  # master 同 Bus 类，一并可用

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Bus":
        return cls(
            name=data.get("name", "master") or "master",
            volume=float(data["volume"]) if data.get("volume") is not None else 1.0,
            pan=float(data["pan"]) if data.get("pan") is not None else 0.0,
            automation=dict(data.get("automation") or {}),
            effects=[Effect.from_dict(e) for e in data.get("effects") or []],
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
class Bookmark:
    """书签（M-V8 E1）：三层作用域的**结构数据**（段轨转正）。

    - scope: "project"（乐段/记号）| "folder"（文件夹层）| "track"（轨道层）
    - ref: folder/track 作用域的目标名（project 作用域 = 空串）
    - kind: "section"（区间，end 必填）| "mark"（时间点记号）
    - start / end: 秒；label: 显示名；color: 可选配色（前端缺省派色）
    """

    scope: str = "project"
    ref: str = ""
    kind: str = "mark"
    start: float = 0.0
    end: float | None = None
    label: str = ""
    color: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Bookmark":
        return cls(
            scope=str(data.get("scope") or "project"),
            ref=str(data.get("ref") or ""),
            kind=str(data.get("kind") or "mark"),
            start=float(data.get("start") or 0.0),
            end=float(data["end"]) if data.get("end") is not None else None,
            label=str(data.get("label") or ""),
            color=str(data.get("color") or ""),
        )


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
    # ---- M-V8 E1 字段（向后兼容增补）----
    bookmarks: list[Bookmark] = field(default_factory=list)  # 书签（项目/文件夹/轨道三层，段轨转正）

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
            bookmarks=[Bookmark.from_dict(b) for b in data.get("bookmarks") or []],
        )
