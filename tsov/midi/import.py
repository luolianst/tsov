"""MIDI → Score 导入（pretty_midi）。M-V8 E5（任务书 §10.1 I）。

- 每 instrument → 一轨：GM program 号反查名（未收录 → piano）；is_drum → "drums"
- tempo / 拍号 / 调号取文件首值（缺省 120 / 4/4 / 无）
- note 映射：pitch_midi=int；pitch_hz=midi_to_hz；velocity=0..1（/127）；confidence=1.0；
  deviation_cents=0；is_ornament=False —— 与 export.score_to_midi 互为回路（velocity×127 逐位一致）

注：模块名 `import` 是关键字，常规 import 语法不可达——调用方经 importlib 访问
（`import_module("tsov.midi.import").midi_to_score`，见 host/project.import_midi）。
"""

from __future__ import annotations

from pathlib import Path

import pretty_midi

from ..core.notes import Note
from ..core.score import Instrument, KeyCandidate, Score, Track
from ..core.units import midi_to_hz
from .export import GM_PROGRAMS

# GM program 号 → 工程音源名（GM_PROGRAMS 反查；同号多名取先声明者，如 27 → guitar）
_NAME_BY_PROGRAM: dict[int, str] = {}
for _name, _num in GM_PROGRAMS.items():
    if _num is not None:
        _NAME_BY_PROGRAM.setdefault(int(_num), _name)

_PITCH_NAMES = ["c", "c#", "d", "d#", "e", "f", "f#", "g", "g#", "a", "a#", "b"]


def program_name(number: int) -> str:
    """GM program 号 → 工程音源名；未收录 → piano（与 export.program_number 互逆）。"""
    return _NAME_BY_PROGRAM.get(int(number), "piano")


def key_name(key_number: int) -> str:
    """MIDI key number（0-11 大调 / 12-23 小调）→ 'C major' / 'a minor'（工程口径小写）。"""
    k = int(key_number) % 24
    if k >= 12:
        return _PITCH_NAMES[k - 12] + " minor"
    return _PITCH_NAMES[k] + " major"


def restore_midi_name(name: str) -> str:
    """轨名还原（对称 export.midi_safe_name；E3 段3 中文轨名回环）。

    「UTF-8 字节经 latin-1 读出」的伪串 → 还原出原 UTF-8 文本；非法序列/常规
    latin-1 名 → 原样返回（ASCII 不受影响）。对外部 MIDI 的 UTF-8 轨名是更正确的恢复。
    """
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def midi_to_score(path: str | Path, *, title: str | None = None) -> Score:
    """读 MIDI 文件 → Score（保序、不裁空轨——空轨由 Project.import_midi 跳过）。"""
    midi = pretty_midi.PrettyMIDI(str(path))

    tempo = 120.0
    _times, tempi = midi.get_tempo_changes()
    if len(tempi):
        tempo = float(tempi[0])

    ts = "4/4"
    if midi.time_signature_changes:
        c = midi.time_signature_changes[0]
        ts = f"{int(c.numerator)}/{int(c.denominator)}"

    keys: list[KeyCandidate] = []
    if midi.key_signature_changes:
        keys = [KeyCandidate(key=key_name(midi.key_signature_changes[0].key_number),
                             confidence=0.5)]

    tracks: list[Track] = []
    for i, inst in enumerate(midi.instruments):
        prog = "drums" if inst.is_drum else program_name(inst.program)
        notes = [
            Note(
                start=round(float(n.start), 6),
                end=round(float(n.end), 6),
                pitch_midi=int(n.pitch),
                pitch_hz=midi_to_hz(int(n.pitch)),
                velocity=max(0.0, min(1.0, float(n.velocity) / 127.0)),
                confidence=1.0,
                deviation_cents=0.0,
                is_ornament=False,
            )
            for n in inst.notes
        ]
        nm = restore_midi_name(str(inst.name or "").strip()) or f"track {i + 1}"
        tracks.append(Track(name=nm, instrument=Instrument(program=prog), notes=notes))

    return Score(
        title=title or Path(path).stem,
        tempo=tempo,
        time_signature=ts,
        key_candidates=keys,
        tracks=tracks,
    )
