"""Score → MIDI 导出（pretty_midi）。M3 填充，M7 多轨化。

- 遍历 score.tracks 全部（每轨一个 pretty_midi.Instrument，GM program 映射）
- 打击乐轨：is_drum=True（channel 9，kick=36/snare=38/hihat=42/crash=49）
- tempo 用 score.tempo（Voice.bpm）；key signature 用第一个调性候选
- 单轨场景行为不变。midi_to_score 反向留 M4。
"""

from __future__ import annotations

import pretty_midi

from ..core.score import Score

# 钢琴 program 号（GM：0 = Acoustic Grand Piano；ADR-0004 回放音色中性钢琴）
PIANO_PROGRAM = 0

# Instrument.program（字符串）→ GM program 号；drums 特殊（打击乐轨）
GM_PROGRAMS = {
    "piano": 0,
    "bass": 33,      # Electric Bass (finger)
    "strings": 48,   # String Ensemble 1
    "pad": 89,       # Pad 2 (warm)
    "drums": None,   # 打击乐：is_drum=True
}

# 打击乐 note（GM channel 9）
DRUM_NOTES = {"kick": 36, "snare": 38, "hihat": 42, "crash": 49}

# 琴键写法 → (key_number)。大调 = 主音半音序号；小调 = 12 + 主音半音序号
_PITCH_CLASS = {
    "c": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3, "e": 4, "f": 5,
    "f#": 6, "gb": 6, "g": 7, "g#": 8, "ab": 8, "a": 9, "a#": 10, "bb": 10, "b": 11,
}


def parse_key_signature(key_str: str) -> int:
    """'C major' / 'a minor' / 'F# major' → MIDI key number（大调 0-11，小调 12-23）。"""
    if not key_str:
        return 0
    key_str = key_str.strip()
    lower = key_str.lower()
    if "minor" in lower:
        root = lower.split("minor")[0].strip()
        return 12 + _PITCH_CLASS.get(root, 0)
    if "major" in lower:
        root = lower.split("major")[0].strip()
        return _PITCH_CLASS.get(root, 0)
    # 兜底：当作根音名（默认大调）
    return _PITCH_CLASS.get(lower.split()[0], 0)


def program_number(program: str) -> int:
    """Instrument.program 字符串 → GM program 号；未知/空 → piano。"""
    if not program:
        return PIANO_PROGRAM
    p = GM_PROGRAMS.get(str(program).strip().lower())
    return PIANO_PROGRAM if p is None else p


def is_drum_track(program: str) -> bool:
    return str(program).strip().lower() == "drums"


def score_to_midi(score: Score, output_path: str) -> str:
    """Score → MIDI 文件，返回产物路径。

    - 遍历全部 tracks：每轨一个 Instrument（GM program 映射；drums → is_drum=True）
    - tempo 用 score.tempo；key signature 用第一个调性候选
    """
    midi = pretty_midi.PrettyMIDI(initial_tempo=float(score.tempo or 120.0))

    for track in score.tracks:
        is_drum = is_drum_track(track.instrument.program)
        prog = 0 if is_drum else program_number(track.instrument.program)
        inst = pretty_midi.Instrument(program=prog, is_drum=is_drum)
        for n in sorted(track.notes, key=lambda n: n.start):
            velocity = max(1, min(127, int(round(float(n.velocity) * 127.0))))
            inst.notes.append(
                pretty_midi.Note(
                    velocity=velocity,
                    pitch=int(n.pitch_midi),
                    start=float(n.start),
                    end=float(n.end),
                )
            )
        midi.instruments.append(inst)

    if score.key_candidates and score.key_candidates[0].key:
        midi.key_signature_changes.append(
            pretty_midi.KeySignature(parse_key_signature(score.key_candidates[0].key), 0)
        )

    midi.write(output_path)
    return output_path
