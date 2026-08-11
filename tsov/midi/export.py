"""Score → MIDI 导出（pretty_midi）。M3 填充实现。

第一版单轨旋律：tempo 用 Voice.bpm，音符 start/end/pitch 直写，
调性候选写入 MIDI key signature（第一个候选）。midi_to_score 反向留 M4。
"""

from __future__ import annotations

import pretty_midi

from ..core.score import Score

# 钢琴 program 号（GM：0 = Acoustic Grand Piano；ADR-0004 回放音色中性钢琴）
PIANO_PROGRAM = 0

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


def score_to_midi(score: Score, output_path: str) -> str:
    """Score → MIDI 文件，返回产物路径。

    - 第一版单轨旋律：取 score.tracks[0]（若空则导出空 MIDI）
    - tempo 用 score.tempo（来自 Voice.bpm）；key signature 用第一个调性候选
    """
    midi = pretty_midi.PrettyMIDI(initial_tempo=float(score.tempo or 120.0))

    if score.tracks:
        track = score.tracks[0]
        inst = pretty_midi.Instrument(program=PIANO_PROGRAM)
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
