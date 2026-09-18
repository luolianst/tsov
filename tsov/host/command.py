"""宿主编辑命令层（ADR-0015）：EditCommand 动词 + EditBatch 事务 + 程序校验。

- 动词第一版：add / remove / set_pitch / set_velocity / set_time / transpose / quantize_time
  （track 级批量 + 单音符精确两种粒度；与 M4「标注精确、LLM 语义、程序校验」同构）
- UI 批A 增补（2026-09-17）：set_tempo（工程级 tempo/拍号） / set_track_mix（volume/pan/mute/solo/bus）
  / set_instrument（program） / add_effect / remove_effect——参数层 UI 与 agent 工具同出，走同一命令通道
- 事务协议：EditBatch.apply(score) 在深拷贝上逐条执行——非法命令被拒绝并记录 error，
  合法命令全部生效（部分应用 + 错误清单）；apply 前不脏原 Score。
- 该命令层 = M-V2 起 agent 工具与 Web UI 共用的编辑通道（docs/05 接口契约）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ..core.notes import Note
from ..core.score import Effect, Score
from ..dsp.pitch import midi_to_hz


@dataclass
class EditCommand:
    op: str  # add / remove / set_pitch / set_velocity / set_time / transpose / quantize_time
    track: int = 0
    index: int | None = None  # 单音符命令的目标下标（add 缺省=末尾追加）
    value: Any = None


@dataclass
class BatchResult:
    ok: bool  # 是否全部命令成功
    applied: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "applied": self.applied, "errors": self.errors}


class EditBatch:
    """一组编辑命令（一个事务 = 一轮 LLM 修改 = 一个 git commit）。"""

    def __init__(self, label: str = ""):
        self.label = label
        self.commands: list[EditCommand] = []

    def add(self, op: str, track: int = 0, index: int | None = None, value: Any = None) -> "EditBatch":
        self.commands.append(EditCommand(op=op, track=track, index=index, value=value))
        return self

    def __len__(self) -> int:
        return len(self.commands)

    def apply(self, score: Score) -> tuple[Score, BatchResult]:
        """在深拷贝上执行全部命令 → (新 Score, 结果)。原 Score 不被修改。"""
        out = copy.deepcopy(score)
        errors: list[str] = []
        applied = 0
        for c in self.commands:
            err = _apply_one(out, c)
            if err:
                errors.append(err)
            else:
                applied += 1
        return out, BatchResult(ok=not errors, applied=applied, errors=errors)


def _apply_one(score: Score, c: EditCommand) -> str | None:
    """执行单命令 → 错误文本（None=成功）。"""
    # ---- 工程级命令（不需要 track）----
    if c.op == "set_tempo":
        return _apply_set_tempo(score, c)
    if not (0 <= c.track < len(score.tracks)):
        return f"{c.op} 越界：track {c.track}（共 {len(score.tracks)} 轨）"
    track = score.tracks[c.track]
    notes = track.notes

    # ---- 轨道级参数命令（UI 批A：参数层与 agent 工具同出）----
    if c.op == "set_track_mix":
        return _apply_set_track_mix(score, track, c)
    if c.op == "set_instrument":
        return _apply_set_instrument(track, c)
    if c.op == "add_effect":
        return _apply_add_effect(track, c)
    if c.op == "remove_effect":
        return _apply_remove_effect(track, c)

    if c.op == "add":
        v = c.value
        if not isinstance(v, dict) or "pitch_midi" not in v:
            return "add 缺 pitch_midi"
        pm = int(v["pitch_midi"])
        start = float(v.get("start", 0.0))
        end = float(v.get("end", start + 0.3))
        if not (0 <= pm <= 127):
            return f"add pitch_midi 非法：{pm}"
        if start >= end:
            return f"add start>=end：{start}/{end}"
        idx = c.index if c.index is not None else len(notes)
        if not (0 <= idx <= len(notes)):
            return f"add 越界：index {idx}（允许 0..{len(notes)}）"
        notes.insert(
            idx,
            Note(
                start=round(start, 6),
                end=round(end, 6),
                pitch_midi=pm,
                pitch_hz=midi_to_hz(pm),
                velocity=round(float(v.get("velocity", 0.8)), 3),
                confidence=round(float(v.get("confidence", 0.8)), 3),
            ),
        )
        return None

    # 单音符命令需要 index；transpose / quantize_time 是轨级批量，不需要
    if c.op in ("remove", "set_pitch", "set_velocity", "set_time"):
        if c.index is None or not (0 <= c.index < len(notes)):
            return f"{c.op} 越界：index {c.index}（共 {len(notes)} 音）"
    i = c.index

    if c.op == "remove":
        notes.pop(i)
        return None
    if c.op == "set_pitch":
        try:
            pm = int(c.value)
        except (TypeError, ValueError):
            return f"set_pitch 非法：{c.value!r}"
        if not (0 <= pm <= 127):
            return f"set_pitch 越界：{pm}"
        n = notes[i]
        n.pitch_midi = pm
        n.pitch_hz = midi_to_hz(pm)
        n.deviation_cents = 0.0
        return None
    if c.op == "set_velocity":
        try:
            v = float(c.value)
        except (TypeError, ValueError):
            return f"set_velocity 非法：{c.value!r}"
        if not (0.0 <= v <= 1.0):
            return f"set_velocity 越界：{v}"
        notes[i].velocity = round(v, 3)
        return None
    if c.op == "set_time":
        v = c.value
        if not isinstance(v, dict):
            return "set_time value 需 {start?, end?}"
        n = notes[i]
        start = float(v.get("start", n.start))
        end = float(v.get("end", n.end))
        if start >= end:
            return f"set_time start>=end：{start}/{end}"
        n.start = round(start, 6)
        n.end = round(end, 6)
        return None
    if c.op == "transpose":
        try:
            st = int(c.value)
        except (TypeError, ValueError):
            return f"transpose 非法：{c.value!r}"
        for n in notes:
            if not (0 <= n.pitch_midi + st <= 127):
                return f"transpose 越界：{n.pitch_midi}+{st}"
        for n in notes:
            n.pitch_midi += st
            n.pitch_hz = midi_to_hz(n.pitch_midi)
        return None
    if c.op == "quantize_time":
        try:
            grid = int(c.value or 16)
        except (TypeError, ValueError):
            return f"quantize_time 非法：{c.value!r}"
        if grid <= 0:
            return f"quantize_time 非法 grid：{grid}"
        beat_s = 60.0 / (score.tempo or 120.0)
        cell_s = beat_s / grid
        for n in notes:
            n.start = round(round(n.start / cell_s) * cell_s, 6)
            n.end = round(round(n.end / cell_s) * cell_s, 6)
            if n.end <= n.start:
                n.end = round(n.start + cell_s, 6)
        return None
    return f"未知 op：{c.op}"


# ======================================================================
# UI 批A：工程级 / 轨道级参数命令（与 agent 工具 set_tempo / set_track_mix 同语义）
# ======================================================================


def _apply_set_tempo(score: Score, c: EditCommand) -> str | None:
    """value = {"tempo"?: number, "time_signature"?: "6/8"}（至少一个）。"""
    v = c.value
    if not isinstance(v, dict):
        return "set_tempo value 需 {tempo?, time_signature?}"
    if v.get("tempo") is not None:
        try:
            t = float(v["tempo"])
        except (TypeError, ValueError):
            return f"set_tempo 非法：{v['tempo']!r}"
        if not (20.0 <= t <= 400.0):
            return f"tempo 越界：{t}（20~400）"
        score.tempo = round(t, 3)
    if v.get("time_signature") is not None:
        from ..core.score import parse_time_signature

        raw = str(v["time_signature"]).strip()
        num, den = parse_time_signature(raw)
        if f"{num}/{den}" != raw:
            return f"time_signature 非法（形如 6/8 / 3/4）：{v['time_signature']!r}"
        score.time_signature = f"{num}/{den}"
    if v.get("tempo") is None and v.get("time_signature") is None:
        return "set_tempo 至少需要 tempo 或 time_signature 之一"
    return None


def _apply_set_track_mix(score: Score, track, c: EditCommand) -> str | None:
    """value = {"volume"?: 0~2, "pan"?: -1~1, "mute"?: bool, "solo"?: bool, "bus"?: name}。"""
    v = c.value
    if not isinstance(v, dict):
        return "set_track_mix value 需 {volume?, pan?, mute?, solo?, bus?}"
    known = ("volume", "pan", "mute", "solo", "bus")
    for k in v:
        if k not in known:
            return f"set_track_mix 未知键：{k!r}（可用：{', '.join(known)}）"
    if v.get("volume") is not None:
        try:
            vol = float(v["volume"])
        except (TypeError, ValueError):
            return f"volume 非法：{v['volume']!r}"
        if not (0.0 <= vol <= 2.0):
            return f"volume 越界：{vol}（0~2）"
        track.instrument.volume = round(vol, 4)
    if v.get("pan") is not None:
        try:
            pan = float(v["pan"])
        except (TypeError, ValueError):
            return f"pan 非法：{v['pan']!r}"
        if not (-1.0 <= pan <= 1.0):
            return f"pan 越界：{pan}（-1~1）"
        track.pan = round(pan, 4)
    if v.get("mute") is not None:
        track.mute = bool(v["mute"])
    if v.get("solo") is not None:
        track.solo = bool(v["solo"])
    if v.get("bus") is not None:
        name = str(v["bus"]).strip() or "master"
        known_buses = {"master"} | {b.name for b in getattr(score, "buses", []) or []}
        if name not in known_buses:
            return f"未知总线：{name!r}（可用：{', '.join(sorted(known_buses))}）"
        track.bus = name
    return None


def _apply_set_instrument(track, c: EditCommand) -> str | None:
    """value = {"program": str}；空串=默认音色；GM 名 / vst3:<路径> / sfz:<路径>。"""
    v = c.value
    if not isinstance(v, dict) or v.get("program") is None:
        return "set_instrument value 需 {program}"
    program = str(v["program"]).strip()
    if program and not program.startswith(("vst3:", "sfz:")):
        from ..midi.export import GM_PROGRAMS

        if program not in GM_PROGRAMS:
            return f"未知音色名 {program!r}（GM 名 / vst3:<路径> / sfz:<路径>，或空串=默认）"
    track.instrument.program = program
    return None


def _apply_add_effect(track, c: EditCommand) -> str | None:
    """value = {"type": kind, "params"?: {...}}；params 省略 = 插件默认参数。"""
    v = c.value
    if not isinstance(v, dict) or not v.get("type"):
        return "add_effect value 需 {type, params?}"
    kind = str(v["type"]).strip()
    from .effect import effect_kinds, validate_effect

    if kind not in effect_kinds():
        return f"未知效果类型：{kind!r}（可用：{', '.join(effect_kinds())}）"
    params = v.get("params") or {}
    if not isinstance(params, dict):
        return "add_effect params 需为对象"
    problems = validate_effect(Effect(type=kind, params=params))
    if problems:
        return "；".join(problems)
    track.instrument.effects.append(Effect(type=kind, params=dict(params)))
    return None


def _apply_remove_effect(track, c: EditCommand) -> str | None:
    """index = 效果链下标（用 index 字段；也接受 value 传数字）。"""
    effects = track.instrument.effects
    idx = c.index if c.index is not None else c.value
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return f"remove_effect 需要 index：{idx!r}"
    if not (0 <= idx < len(effects)):
        return f"remove_effect 越界：index {idx}（共 {len(effects)} 个效果）"
    effects.pop(idx)
    return None
