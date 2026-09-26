"""配器展开器（M-V8 E4 段3 · Q14 三段一库之⑤）：决策（变体 ID / fill ID / 强度）→ 音符。

红线（Q14）：LLM 只选 ID；「哪小节哪格、什么音高、多长」全部由本模块机械展开——
- token（root/third/fifth/seventh/octave/chord/root5/arp:i）按**逐小节和弦**解析，寄存器内定位
- 力度 = 模板 vel × 能量档 vel_scale × 强度档 strengths；≤1.0 截顶
- 旋律跟随变体（melody_offset）复制旋律轨音符（±12 半音）
- fill：段末小节叠加 fills 库事件；section_crash：段头 crash（非首段、鼓有音符时）
纯函数、无宿主依赖。输入事实见 tsov.arrange.facts。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.names import note_name_to_midi
from ..core.notes import Note
from ..core.units import midi_to_hz
from ..midi.export import GM_PROGRAMS
from . import harmony as _harmony
from .library import PatternLibrary, parse_token

__all__ = ["ExpandResult", "expand_role_section", "gm_name_for_number", "role_program", "role_track_name"]


@dataclass
class ExpandResult:
    """单角色 × 单段的展开产物。"""

    role: str
    track_name: str
    program: str                  # Instrument.program（GM 名；"drums" = 打击乐）
    variant: str
    melody_offset: int | None
    start_bar: int                # 1-based 全局小节（含）
    end_bar: int
    fill: str | None = None
    crash: bool = False
    notes: list[Note] = field(default_factory=list)


def gm_name_for_number(num: int) -> str:
    """GM program 号 → 工程内 Instrument.program 名（GM_PROGRAMS 首个匹配；未收录 → piano）。"""
    for nm, n in GM_PROGRAMS.items():
        if n == num:
            return nm
    return "piano"


def role_program(lib: PatternLibrary, role: str) -> str:
    """角色 → Instrument.program（gm_program=None → "drums"）。"""
    g = lib.role(role).get("gm_program")
    return "drums" if g is None else gm_name_for_number(int(g))


def role_track_name(lib: PatternLibrary, role: str) -> str:
    """角色 → 轨道名（标题去全角括号注解）。"""
    title = str(lib.role(role).get("title") or role)
    return title.split("（", 1)[0].strip() or role


# ---------------------------------------------------------------------------
# 音高解析（token × 和弦 × 寄存器）
# ---------------------------------------------------------------------------


def _chord_intervals(root_pc: int, tones, kind: str) -> list[int]:
    """和弦构成音相对根音的音程（升序，含 0）。"""
    base = sorted({(int(t) - int(root_pc)) % 12 for t in (tones or [])})
    if not base:
        base = sorted(_harmony.TRIAD.get(str(kind), (0, 4, 7)))
    if 0 not in base:
        base = [0] + [x for x in base if x != 0]
    return base


def _place_root(root_pc: int, lo: int, hi: int) -> int:
    """根音落到寄存器中心最近八度（再做 lo/hi 收拢）。"""
    center = (lo + hi) / 2.0
    p = int(root_pc) + 12 * int(round((center - int(root_pc)) / 12.0))
    while p < lo:
        p += 12
    while p - 12 >= lo and p > hi:
        p -= 12
    return p


def _fit_register(p: int, lo: int, hi: int) -> int:
    while p > hi and p - 12 >= lo:
        p -= 12
    while p < lo and p + 12 <= 127:
        p += 12
    return p


def _resolve_token(tok: str, *, root_pc: int, intervals: list[int], lo: int | None, hi: int | None) -> list[int]:
    """token → 音高列表（rest → []；绝对音高直通；相对音高走和弦+寄存器）。"""
    t = parse_token(tok)
    if t["kind"] == "rest":
        return []
    if t["kind"] == "abs":
        return [int(t["pitch"])]
    if lo is None or hi is None:
        raise ValueError(f"相对 token {tok!r} 需要角色 register（当前为 null）")
    off = int(t.get("offset") or 0)
    kind = t["kind"]
    if kind == "chord":
        ivs = list(intervals)
    elif kind == "root5":
        ivs = [intervals[0], intervals[2] if len(intervals) > 2 else intervals[-1]]
    elif kind == "arp":
        ivs = [intervals[int(t.get("arp_idx") or 0) % len(intervals)]]
    elif kind in ("root", "third", "fifth", "seventh", "octave"):
        pos = {"root": 0, "third": 1, "fifth": 2, "seventh": 3, "octave": 0}[kind]
        iv = intervals[pos] if pos < len(intervals) else intervals[-1]
        ivs = [iv + 12 if kind == "octave" else iv]
    else:  # pragma: no cover —— 语法已由库校验兜底
        raise ValueError(f"未知 token kind：{kind}")
    root = _place_root(root_pc, lo, hi)
    return [_fit_register(root + int(iv) + off, lo, hi) for iv in ivs]


# ---------------------------------------------------------------------------
# 展开
# ---------------------------------------------------------------------------


def expand_role_section(lib: PatternLibrary, facts: dict, sec_index: int, role: str, variant_id: str, *,
                        strength: str | None = None, fill: str | None = None,
                        melody_notes: list[Note] | None = None) -> ExpandResult:
    """「角色 × 段 × 变体」展开为音符。"""
    sec = facts["sections"][int(sec_index)]
    strength = str(strength or facts.get("strength") or "standard")
    if strength not in lib.strengths:
        raise ValueError(f"strength 非法：{strength!r}（可用 {sorted(lib.strengths)}）")
    var = lib.variant(role, variant_id)
    spec = lib.role(role)
    program = role_program(lib, role)
    res = ExpandResult(role=role, track_name=role_track_name(lib, role), program=program,
                       variant=str(variant_id), melody_offset=None,
                       start_bar=int(sec["start_bar"]), end_bar=int(sec["end_bar"]), fill=fill)
    off = var.get("melody_offset")

    if off is not None:
        # 旋律跟随：复制段内旋律音符（±12 半音）
        if not melody_notes:
            raise ValueError(f"{role}.{variant_id} 为旋律跟随变体，需要 melody_notes 传入")
        res.melody_offset = int(off)
        s, e = float(sec["start"]), float(sec["end"])
        for n in melody_notes:
            if s - 1e-9 <= n.start < e - 1e-9:
                p = int(n.pitch_midi) + int(off)
                if 0 <= p <= 127:
                    res.notes.append(Note(start=round(float(n.start), 6), end=round(float(n.end), 6),
                                          pitch_midi=p, pitch_hz=midi_to_hz(p),
                                          velocity=round(min(1.0, max(0.0, float(n.velocity))), 3),
                                          confidence=0.85))
        res.notes.sort(key=lambda x: (x.start, x.pitch_midi))
        return res

    rng = spec.get("register")
    lo = hi = None
    if rng is not None:
        lo, hi = note_name_to_midi(rng[0]), note_name_to_midi(rng[1])
    vel_mult = float(lib.strengths[strength]) * float(lib.energy_tiers[str(var["energy"])]["vel_scale"])

    grid_sec = float(facts["meter"]["grid_sec"])
    bar_start_by_i = {int(b["i"]): float(b["start"]) for b in facts["bars"]}
    chords_by_bar = {int(r["bar"]): r for r in facts["chords"]["per_bar"]}
    events = list(var.get("events") or [])
    fill_events: list[list] = []
    if fill:
        fdef = lib.fill(fill)
        if str(fdef.get("role")) != str(role):
            raise ValueError(f"fill {fill!r} 属于角色 {fdef.get('role')!r}，不能用在 {role!r}")
        fill_events = [list(e[1:]) for e in fdef["events"]]  # 去 bar_offset（v1 全 0）

    span_start, span_end = float(sec["start"]), float(sec["end"])
    for b in range(res.start_bar, res.end_bar + 1):
        bstart = bar_start_by_i.get(b)
        if bstart is None:
            continue
        ch = chords_by_bar.get(b)
        evs = events + fill_events if (fill_events and b == res.end_bar) else events
        for grid, ln, vel, tok in evs:
            t0 = bstart + (grid - 1) * grid_sec
            t0 = max(t0, span_start)
            if t0 >= span_end - 1e-9:
                continue
            t1 = min(bstart + (grid - 1 + ln) * grid_sec, span_end)
            if t1 <= t0 + 1e-9:
                continue
            if tok in ("rest",):
                continue
            if ch is None:
                continue
            intervals = _chord_intervals(int(ch["root_pc"]), ch.get("tones"), str(ch.get("kind")))
            for p in _resolve_token(tok, root_pc=int(ch["root_pc"]), intervals=intervals, lo=lo, hi=hi):
                if 0 <= p <= 127:
                    res.notes.append(Note(start=round(t0, 6), end=round(t1, 6), pitch_midi=int(p),
                                          pitch_hz=midi_to_hz(int(p)),
                                          velocity=round(min(1.0, max(0.0, float(vel) * vel_mult)), 3),
                                          confidence=0.9))

    # 段头 crash（非首段；仅鼓角色；鼓已有音符才加）
    crash_def = lib.section_crash
    if crash_def and program == "drums" and res.notes and int(sec["index"]) > 0:
        t0 = span_start
        t1 = min(span_start + 2 * grid_sec, span_end)
        res.crash = True
        res.notes.append(Note(start=round(t0, 6), end=round(t1, 6), pitch_midi=int(crash_def["pitch"]),
                              pitch_hz=midi_to_hz(int(crash_def["pitch"])),
                              velocity=round(min(1.0, float(crash_def["vel"]) * float(lib.strengths[strength])), 3),
                              confidence=0.9))

    res.notes.sort(key=lambda x: (x.start, x.pitch_midi))
    return res
