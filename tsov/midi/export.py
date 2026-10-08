"""Score → MIDI 导出（pretty_midi）。M3 填充，M7 多轨化。

- 遍历 score.tracks 全部（每轨一个 pretty_midi.Instrument，GM program 映射）
- 打击乐轨：is_drum=True（channel 9，kick=36/snare=38/hihat=42/crash=49）
- tempo 用 score.tempo（Voice.bpm）；key signature 用第一个调性候选；拍号用 score.time_signature（缺省 4/4）
- 单轨场景行为不变。midi_to_score 反向留 M4。
"""

from __future__ import annotations

import math

import pretty_midi

from ..core.score import Score, parse_time_signature

# 钢琴 program 号（GM：0 = Acoustic Grand Piano；ADR-0004 回放音色中性钢琴）
PIANO_PROGRAM = 0

# Instrument.program（字符串）→ GM program 号；drums 特殊（打击乐轨）
GM_PROGRAMS = {
    "piano": 0,
    "e_piano": 4,       # Electric Piano 1（Rhodes 系）
    "guitar": 27,       # Electric Guitar (clean)
    "guitar_clean": 27,
    "guitar_acoustic": 25,  # Acoustic Guitar (steel) —— E4 段3 配器角色用（2026-09-26 增补）
    "guitar_muted": 28, # Electric Guitar (muted) —— 哑音 riff
    "guitar_overdrive": 29,
    "guitar_distortion": 30,
    "bass": 33,      # Electric Bass (finger)
    "synth_bass": 38,  # Synth Bass 1
    "strings": 48,   # String Ensemble 1
    # 管弦乐单件（2026-09-21 增补 · geyun 接口测试预置；纯新增、不改旧名）
    "violin": 40,       # GM#41 Violin
    "viola": 41,        # GM#42 Viola
    "cello": 42,        # GM#43 Cello
    "contrabass": 43,   # GM#44 Contrabass
    "harp": 46,         # GM#47 Orchestral Harp
    "timpani": 47,      # GM#48 Timpani
    "flute": 73,        # GM#74 Flute
    "oboe": 68,         # GM#69 Oboe
    "clarinet": 71,     # GM#72 Clarinet
    "bassoon": 70,      # GM#71 Bassoon
    "french_horn": 60,  # GM#61 French Horn
    "trumpet": 56,      # GM#57 Trumpet
    "trombone": 57,     # GM#58 Trombone
    "organ": 16,       # Drawbar Organ
    "brass": 61,       # Brass Section
    "synth_lead": 81,  # Lead 2 (sawtooth)
    "synth_lead_square": 80,   # Lead 1 (square)
    "synth_lead_charang": 85,  # Lead 5 (charang)
    "synth_arp": 82,   # Lead 3 (calliope) —— E4 段3 配器角色（琶音/pluck 层；2026-09-26 增补）
    "pad": 89,       # Pad 2 (warm)
    "pad_polysynth": 90,       # Pad 3 (polysynth)
    "drums": None,   # 打击乐：is_drum=True
}

# 打击乐 note（GM channel 9）
DRUM_NOTES = {"kick": 36, "snare": 38, "hihat": 42, "crash": 49}

# GM 打击乐 bank：channel 9 的鼓组在 bank 128（bank 0 / preset 0 = 大钢琴——
# 2026-09-27 修复前，SF2Source 曾对鼓轨显式选 bank0 →「内置 drum 实为钢琴」）
DRUM_BANK = 128

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


def midi_safe_name(name: str) -> str:
    """SMF meta 文本编码防护（E3 段3 修复：中文轨名导出曾 UnicodeEncodeError）。

    mido/pretty_midi 以 latin-1 编码 meta 文本。latin-1 无法表示的名字（如中文）→
    转 UTF-8 字节再以 latin-1 伪串承载（写出字节即 UTF-8，主流 DAW 按 UTF-8 读回正确
    中文）。导入侧 restore_midi_name 对称还原。latin-1 原生可表示的名（ASCII/西欧）
    原样返回——行为不变。
    """
    try:
        name.encode("latin-1")
        return name
    except UnicodeEncodeError:
        return name.encode("utf-8").decode("latin-1")


# ---- v0.2 批C 后段（挂道族）：perf 曲线 → MIDI 事件（断点 + 线性加密） ----

_PERF_CC = {"cc1": 1, "cc11": 11, "cc64": 64}
_PERF_STEP_V = 2.0 / 128.0        # 值变化阈值（约 2 级 CC，防事件爆量/丢包络）


def _perf_step_t(bpm: float) -> float:
    """相邻加密点最大间隔 = 1/32 拍（秒）。"""
    return (60.0 / max(1.0, float(bpm or 120.0))) / 32.0


def _densify_points(points, bpm: float) -> list[tuple[float, float]]:
    """[[t, v], …] → 断点 + 线性加密序列（Δv ≥ 2/128 或每 1/32 拍补点；单段 ≤512 步）。"""
    pts = [(float(p[0]), float(p[1])) for p in (points or [])
           if p is not None and len(p) >= 2]
    if not pts:
        return []
    step_t = _perf_step_t(bpm)
    out: list[tuple[float, float]] = [pts[0]]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        dt, dv = t1 - t0, v1 - v0
        if dt <= 0:
            out.append((t1, v1))
            continue
        n = max(1, int(math.ceil(abs(dv) / _PERF_STEP_V)), int(math.ceil(dt / step_t)))
        n = min(n, 512)
        for k in range(1, n):
            out.append((t0 + dt * k / n, v0 + dv * k / n))
        out.append((t1, v1))
    return out


def _append_perf_events(inst, track, bpm: float) -> None:
    """该轨 automation 的 perf 键（bend / cc1 / cc11 / cc64）→ pretty_midi 事件（按时间排序追加）。

    - bend：v ∈ [-1, 1] → pitch_bend ∈ [-8192, 8191]（SoundFont 默认弯音程 ±2 半音）
    - CC：v ∈ [0, 1] → 0..127
    - 无 perf 数据 = 零事件（旧工程同构，逐字回归）
    """
    auto = getattr(track, "automation", None) or {}
    ev: list[tuple[float, str, int]] = []
    for t, v in _densify_points(auto.get("bend"), bpm):
        ev.append((t, "bend", max(-8192, min(8191, int(round(v * 8192))))))
    for key, cc in _PERF_CC.items():
        for t, v in _densify_points(auto.get(key), bpm):
            ev.append((t, f"cc{cc}", max(0, min(127, int(round(v * 127))))))
    ev.sort(key=lambda x: x[0])
    for t, kind, val in ev:
        if kind == "bend":
            inst.pitch_bends.append(pretty_midi.PitchBend(int(val), float(t)))
        else:
            inst.control_changes.append(pretty_midi.ControlChange(int(kind[2:]), int(val), float(t)))


def score_to_midi(score: Score, output_path: str) -> str:
    """Score → MIDI 文件，返回产物路径。

    - 遍历全部 tracks：每轨一个 Instrument（GM program 映射；drums → is_drum=True；轨名写入
      instrument name——回环导入保名）
    - tempo 用 score.tempo；key signature 用第一个调性候选；拍号用 score.time_signature
    """
    midi = pretty_midi.PrettyMIDI(initial_tempo=float(score.tempo or 120.0))

    for track in score.tracks:
        is_drum = is_drum_track(track.instrument.program)
        prog = 0 if is_drum else program_number(track.instrument.program)
        inst = pretty_midi.Instrument(program=prog, is_drum=is_drum,
                                      name=midi_safe_name(str(track.name or "")))
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
        _append_perf_events(inst, track, float(score.tempo or 120.0))   # v0.2 批C 后段：perf 曲线 → CC/弯音事件
        midi.instruments.append(inst)

    if score.key_candidates and score.key_candidates[0].key:
        midi.key_signature_changes.append(
            pretty_midi.KeySignature(parse_key_signature(score.key_candidates[0].key), 0)
        )

    ts_num, ts_den = parse_time_signature(getattr(score, "time_signature", None))
    midi.time_signature_changes.append(
        pretty_midi.TimeSignature(numerator=ts_num, denominator=ts_den, time=0)
    )

    midi.write(output_path)
    return output_path
