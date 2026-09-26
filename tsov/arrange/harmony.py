"""自动配器：和声轨 + 低音轨 + 打击乐轨（M7 方向 A：配器 + 节奏）。

第一版 style 只做 "pop"：
- harmonize：按小节（4 拍）分块，候选和弦取音阶 1/4/5 级三和弦（2/6 级兜底），选与块内旋律重合度最高
- bassline：和弦根音，八度定位在旋律下方 ~1 个八度
- drums：四四拍 pop 基础 pattern（kick 1/3 拍、snare 2/4 拍、hihat 8 分音符）
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.notes import Note
from ..core.units import midi_to_hz

# 调式 → 音阶 pitch class（相对根音）
SCALES = {
    "major": {0, 2, 4, 5, 7, 9, 11},
    "minor": {0, 2, 3, 5, 7, 8, 10},
    "dorian": {0, 2, 3, 5, 7, 9, 10},
}

# 三和弦 tone 音程（相对根音）
TRIAD = {"major": (0, 4, 7), "minor": (0, 3, 7)}

# 候选级数（相对根音的音程）：1/4/5 优先，2/6 兜底
DEGREES = (0, 5, 7, 2, 9)

# 音名 → pitch class
_PC = {
    "c": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3, "e": 4, "f": 5,
    "f#": 6, "gb": 6, "g": 7, "g#": 8, "ab": 8, "a": 9, "a#": 10, "bb": 10, "b": 11,
}


@dataclass
class Chord:
    """一个和弦：根音 pitch class + 性质 + 时值 + 实际 tone 集合（音阶内取音）。"""

    root_pc: int
    kind: str  # "major" / "minor"
    start: float
    end: float
    tones: set[int] = None  # 构成音 pitch class（音阶内三度/五度）


def parse_key(key_str: str) -> tuple[int, set[int]]:
    """'D major' / 'b minor' / 'd dorian' → (root_pc, scale_pcs)。默认大调。"""
    key_str = (key_str or "").strip().lower()
    if not key_str:
        return 0, SCALES["major"]
    parts = key_str.split()
    root = _PC.get(parts[0], 0)
    mode = parts[1] if len(parts) > 1 else "major"
    scale = SCALES.get(mode, SCALES["major"])
    return root, set((r + root) % 12 for r in scale)


def _scale_chord(root_pc: int, scale: set[int]) -> tuple[str, set[int]]:
    """音阶内根音的三和弦：构成音 = 音阶的第 0/2/4 级（根音、三度、五度）——只用音阶内音。

    例 D dorian 上 B：B-D-F（含 F 自然音，不含调外的 F#）。
    返回 (kind, tones)；kind 按三度大小（minor/major，音阶内五度不一定大三/小三，取近似）。
    """
    scale_sorted = sorted(scale)
    idx = scale_sorted.index(root_pc)
    tones = {scale_sorted[(idx + k) % len(scale_sorted)] for k in (0, 2, 4)}
    third = scale_sorted[(idx + 2) % len(scale_sorted)]
    kind = "minor" if (third - root_pc) % 12 == 3 else "major"
    return kind, tones


def _candidates(tonic: int, scale: set[int]) -> list[tuple[int, str, set[int]]]:
    """音阶 1/4/5/2/6 级（按半音偏移，tonic 为主音）候选和弦（绝对 pitch class）。"""
    cands = []
    for d in DEGREES:
        root = (tonic + d) % 12
        if root in scale:
            kind, tones = _scale_chord(root, scale)
            cands.append((root, kind, tones))
    return cands


def harmonize(melody_notes: list[Note], key: str, bpm: float, **params) -> list[Chord]:
    """旋律 → 小节和弦序列。

    - 每 4 拍一小节（bpm 换算秒）；每块取旋律音 pitch class 集合
    - 候选 = 音阶 1/4/5/2/6 级三和弦，选与块内旋律重合度最高者
    """
    if not melody_notes:
        return []
    root_pc, scale = parse_key(key)
    cands = _candidates(root_pc, scale)
    beat_s = 60.0 / max(float(bpm), 40.0)
    bar_s = beat_s * 4.0
    total = float(max(n.end for n in melody_notes))

    chords: list[Chord] = []
    t = 0.0
    while t < total - 1e-9:
        end = t + bar_s
        window = [n for n in melody_notes if n.start >= t and n.start < end]
        pcs = {int(n.pitch_midi) % 12 for n in window}
        if not pcs:
            if chords:
                prev = chords[-1]
                chords.append(Chord(prev.root_pc, prev.kind, t, min(end, total), set(prev.tones)))
            else:
                kind, tones = _scale_chord(root_pc, scale)
                chords.append(Chord(root_pc, kind, t, min(end, total), tones))
        else:
            best, best_score = None, -1
            for root, kind, tones in cands:
                score = len(pcs & tones)
                if score > best_score:
                    best, best_score = (root, kind, tones), score
            chords.append(Chord(best[0], best[1], t, min(end, total), best[2]))
        t = end
    return chords


def harmonize_bars(melody_notes: list[Note], key: str, bars: list[tuple[float, float]], **params) -> list[Chord]:
    """按显式小节切分（[(start, end), ...]，任意拍号）逐小节选和弦——harmonize 的泛化版。

    6/8（每小节 3 拍）等非 4/4 工程由调用方给出真实小节边界；选择逻辑与 harmonize 相同：
    1/4/5/2/6 级候选，取与小节内旋律重合度最高者；空窗小节沿用前值和弦。
    """
    if not bars:
        return []
    root_pc, scale = parse_key(key)
    cands = _candidates(root_pc, scale)
    chords: list[Chord] = []
    for s, e in bars:
        window = [n for n in melody_notes if n.start >= float(s) - 1e-9 and n.start < float(e) - 1e-9]
        pcs = {int(n.pitch_midi) % 12 for n in window}
        if not pcs:
            if chords:
                prev = chords[-1]
                chords.append(Chord(prev.root_pc, prev.kind, float(s), float(e), set(prev.tones)))
            else:
                kind, tones = _scale_chord(root_pc, scale)
                chords.append(Chord(root_pc, kind, float(s), float(e), tones))
        else:
            best, best_score = None, -1
            for root, kind, tones in cands:
                score = len(pcs & tones)
                if score > best_score:
                    best, best_score = (root, kind, tones), score
            chords.append(Chord(best[0], best[1], float(s), float(e), best[2]))
    return chords


def chord_notes(chord: Chord, target: float = 48.0, velocity: float = 0.5) -> list[Note]:
    """和弦 → 3 个 Note（构成音，紧致 voicing 在 target 附近，跨整个小节）。

    - chord.tones 是绝对 pitch class；根音先落到 target 最近八度，其余 tone 取同八度或上一八度
      （紧凑 voicing：Dm 在 target≈38 → D2/F2/A2 = 38/41/45）
    - pitch_hz 照填（ADR-0005 完整性；勿再加 root_pc 双加）
    """
    root_base = chord.root_pc
    k = round((float(target) - root_base) / 12.0)
    root_pitch = root_base + 12 * k
    tones = sorted(chord.tones or TRIAD[chord.kind])
    out: list[Note] = []
    for iv in tones:
        p = iv + 12 * k
        if p < root_pitch:
            p += 12
        out.append(Note(start=chord.start, end=chord.end, pitch_midi=int(p),
                        pitch_hz=midi_to_hz(p), velocity=velocity, confidence=0.8))
    return out


def bassline(chords: list[Chord], melody_mean: float = 60.0, velocity: float = 0.7, **params) -> list[Note]:
    """和弦根音低音轨：八度定位在旋律均值下方 ~1 个八度，整小节根音。"""
    notes = []
    target = float(melody_mean) - 12.0
    for c in chords:
        k = round((target - c.root_pc) / 12.0)
        root_midi = c.root_pc + 12 * k
        if root_midi < 28:
            root_midi += 12
        if root_midi > 60:
            root_midi -= 12
        notes.append(Note(start=c.start, end=c.end, pitch_midi=int(root_midi),
                          pitch_hz=midi_to_hz(root_midi), velocity=velocity, confidence=0.8))
    return notes


def drums(chords: list[Chord], style: str = "pop", velocity: float = 0.8, **params) -> list[Note]:
    """四四拍 pop 打击乐：kick 1/3 拍、snare 2/4 拍、hihat 8 分音符。"""
    if not chords:
        return []
    kick, snare, hihat = 36, 38, 42
    notes: list[Note] = []
    for c in chords:
        beat_s = (c.end - c.start) / 4.0
        for b in range(4):
            t = c.start + b * beat_s
            if b % 2 == 0:
                notes.append(Note(start=t, end=t + beat_s * 0.45, pitch_midi=kick, pitch_hz=midi_to_hz(kick), velocity=velocity, confidence=0.9))
            else:
                notes.append(Note(start=t, end=t + beat_s * 0.45, pitch_midi=snare, pitch_hz=midi_to_hz(snare), velocity=velocity, confidence=0.9))
            for e in range(2):
                ht = t + e * beat_s * 0.5
                notes.append(Note(start=ht, end=ht + beat_s * 0.35, pitch_midi=hihat, pitch_hz=midi_to_hz(hihat), velocity=velocity * 0.6, confidence=0.9))
    return notes


def arrange(score, style: str = "pop", key: str | None = None, **params):
    """单轨旋律 Score → 多轨配器 Score（melody + harmony + bass + drums）。

    - key 缺省取 score.key_candidates[0]（无则 C major）
    - style 第一版只做 "pop"
    - 返回新 Score（不改原对象）；ADR-0005 schema 字段不变，只填值
    """
    from copy import deepcopy

    from ..core.score import Instrument, KeyCandidate, Track

    melody = score.tracks[0] if score.tracks else None
    melody_notes = list(melody.notes) if melody else []
    if not melody_notes:
        raise ValueError("Score 无旋律音符（无法配器）")

    key_str = key or (score.key_candidates[0].key if score.key_candidates else "C major")
    bpm = float(score.tempo or 120.0)
    melody_mean = float(np.mean([n.pitch_midi for n in melody_notes]))

    chords = harmonize(melody_notes, key_str, bpm)
    bass_notes = bassline(chords, melody_mean)
    drum_notes = drums(chords, style=style)
    harm_target = melody_mean - 12.0  # 和弦 voicing 中心：旋律下方 1 个八度
    harm_notes: list[Note] = []
    for c in chords:
        harm_notes.extend(chord_notes(c, target=harm_target))

    new = deepcopy(score)
    if key:
        new.key_candidates = [KeyCandidate(key=key_str, confidence=1.0)]
    new.tracks = [
        Track(name="melody", instrument=Instrument(backend="fluidsynth", program="piano", volume=0.9), notes=melody_notes),
        Track(name="harmony", instrument=Instrument(backend="fluidsynth", program="strings", volume=0.45), notes=harm_notes),
        Track(name="bass", instrument=Instrument(backend="fluidsynth", program="bass", volume=0.7), notes=bass_notes),
        Track(name="drums", instrument=Instrument(backend="fluidsynth", program="drums", volume=0.7), notes=drum_notes),
    ]
    new.meta = {**score.meta, "arranged": True, "arrange_style": style, "arrange_key": key_str}
    return new
