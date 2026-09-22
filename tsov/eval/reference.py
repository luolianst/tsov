"""参考谱对比评估——M2 实现（ADR-0006 接口冻结，字段不许改）。

主指标（M2 定胜负）：音符级精确率 / 召回率 / F1，配合时值对齐误差。
铁律：reference 真值必须独立于候选底座（禁止用 crepe_notes / basic-pitch 自扒参考谱）。

另含主旋律提取（extract_melody）：多声部转录音符 → 单旋律线（试跑最大发现，
basic-pitch 全声部转录后必须先取主旋律再与单旋律 MIDI 对比）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..core.notes import Note, Voice
from ..core.score import Score
from ..core.units import midi_to_hz


@dataclass
class ReferenceMetrics:
    precision: float = 0.0  # 精确率（转出的音符里对的占比）
    recall: float = 0.0  # 召回率（参考谱里被抓到的占比）
    f1: float = 0.0
    onset_error_ms: float = 0.0  # onset 对齐误差（平均 |hyp-start - ref-start|）
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def load_reference_notes(reference: Score | Sequence[Note] | str | Path) -> list[Note]:
    """把 reference 归一化为 Note 列表。

    支持：
    - Score：取所有 track 的全部音符
    - Sequence[Note]：直接用
    - 字符串/Path：MIDI 文件（pretty_midi）或 JSON 文件（[{start,end,pitch_midi,...}]）
    """
    if isinstance(reference, Score):
        return [n for tr in reference.tracks for n in tr.notes]
    if isinstance(reference, (str, Path)):
        path = Path(reference)
        if path.suffix.lower() in (".mid", ".midi"):
            return _midi_to_notes(path)
        if path.suffix.lower() == ".json":
            return _json_to_notes(path)
        raise ValueError(f"不支持的 reference 文件类型：{path.suffix}（支持 .mid/.midi/.json）")
    return list(reference)


def _midi_to_notes(path: Path) -> list[Note]:
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(str(path))
    notes = []
    for inst in pm.instruments:
        for n in inst.notes:
            notes.append(
                Note(
                    start=float(n.start),
                    end=float(n.end),
                    pitch_midi=int(n.pitch),
                    pitch_hz=float(midi_to_hz(n.pitch)),
                    velocity=max(0.0, min(1.0, float(n.velocity) / 127.0)),
                    confidence=1.0,
                )
            )
    notes.sort(key=lambda x: x.start)
    return notes


def _json_to_notes(path: Path) -> list[Note]:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    notes = []
    for item in data:
        pitch_midi = int(item.get("pitch_midi", item.get("pitch")))
        notes.append(
            Note(
                start=float(item["start"]),
                end=float(item["end"]),
                pitch_midi=pitch_midi,
                pitch_hz=float(item.get("pitch_hz", midi_to_hz(pitch_midi))),
                velocity=float(item.get("velocity", item.get("confidence", 0.8))),
                confidence=float(item.get("confidence", 1.0)),
            )
        )
    notes.sort(key=lambda x: x.start)
    return notes


def extract_melody(notes: Sequence[Note], min_duration: float = 0.08) -> list[Note]:
    """多声部转录音符 → 单主旋律线（soprano line）。

    滑动扫描：任意时刻取"当前发声音符中音高最高者"作为旋律，生成单声道旋律线；
    旋律音高切换处即为音符边界。这是多声部素材（合唱/阿卡贝拉）对比前的必需步骤。
    """
    if not notes:
        return []
    events: list[tuple[float, int, int, int]] = []  # (time, type 0=end/1=start, pitch, idx)
    for i, n in enumerate(notes):
        events.append((n.start, 1, n.pitch_midi, i))
        events.append((n.end, 0, n.pitch_midi, i))
    events.sort(key=lambda e: (e[0], e[1]))  # 同刻先处理 end

    by_time: dict[float, list[tuple[int, int, int]]] = {}
    for t, typ, p, i in events:
        by_time.setdefault(t, []).append((typ, p, i))

    active: dict[int, int] = {}  # idx -> pitch_midi
    melody: list[tuple[float, float, int]] = []
    cur_start: float | None = None
    cur_pitch: int | None = None
    last_t = 0.0

    for t in sorted(by_time):
        for typ, p, i in by_time[t]:
            if typ == 1:
                active[i] = p
            else:
                active.pop(i, None)
        last_t = t
        top = max(active.values()) if active else None
        if top is None:
            if cur_start is not None:
                melody.append((cur_start, t, cur_pitch))
                cur_start = None
                cur_pitch = None
        else:
            if cur_start is None:
                cur_start = t
                cur_pitch = top
            elif top != cur_pitch:
                if t - cur_start >= min_duration:
                    melody.append((cur_start, t, cur_pitch))
                cur_start = t
                cur_pitch = top
    if cur_start is not None:
        melody.append((cur_start, max(last_t, cur_start + min_duration), cur_pitch))

    result = [
        Note(
            start=float(s),
            end=float(e),
            pitch_midi=int(p),
            pitch_hz=float(midi_to_hz(p)),
            velocity=0.8,
            confidence=1.0,
        )
        for s, e, p in melody
        if e - s >= min_duration
    ]
    return result


def align_notes(ref_notes: list[Note], hyp_notes: list[Note], onset_window_s: float = 0.10, pitch_tol: int = 0):
    """贪心对齐：每个参考音符找窗口内音高容差内的最佳候选（同 trial-run.py）。

    返回 (tp, alignments, matched_hyp_indices)。
    """
    used: set[int] = set()
    tp = 0
    alignments = []
    for r in ref_notes:
        best = None
        for i, h in enumerate(hyp_notes):
            if i in used:
                continue
            if abs(h.start - r.start) <= onset_window_s and abs(h.pitch_midi - r.pitch_midi) <= pitch_tol:
                if best is None or abs(h.start - r.start) < abs(best[1].start - r.start):
                    best = (i, h)
        if best:
            used.add(best[0])
            tp += 1
            alignments.append(
                {
                    "ref_start": round(r.start, 3),
                    "ref_pitch": r.pitch_midi,
                    "hyp_start": round(best[1].start, 3),
                    "hyp_pitch": best[1].pitch_midi,
                }
            )
    return tp, alignments, used


def compare_to_reference(voice: Voice, reference: Score | Sequence[Note], **params) -> ReferenceMetrics:
    """转录结果 vs 参考谱（ground truth）：音符级精确率/召回率 + 时值对齐误差。

    params：
    - onset_window_s：onset 容差（默认 0.10s）
    - pitch_tol：音高容差半音（默认 0）
    - extract_melody_first：先做主旋律提取再对比（多声部素材，默认 True）
    """
    onset_window_s = params.get("onset_window_s", 0.10)
    pitch_tol = int(params.get("pitch_tol", 0))
    extract_first = bool(params.get("extract_melody_first", True))

    hyp_notes = extract_melody(voice.notes) if extract_first else list(voice.notes)
    ref_notes = load_reference_notes(reference)

    tp, alignments, _ = align_notes(ref_notes, hyp_notes, onset_window_s, pitch_tol)
    precision = tp / len(hyp_notes) if hyp_notes else 0.0
    recall = tp / len(ref_notes) if ref_notes else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    onset_error_ms = (
        sum(abs(a["hyp_start"] - a["ref_start"]) for a in alignments) / len(alignments) * 1000.0 if alignments else 0.0
    )

    detail = {
        "ref_notes": len(ref_notes),
        "hyp_notes_raw": len(voice.notes),
        "hyp_notes_after_melody": len(hyp_notes),
        "tp": tp,
        "melody_extraction": extract_first,
        "alignments": alignments[:200],
    }
    return ReferenceMetrics(
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        onset_error_ms=round(onset_error_ms, 1),
        detail=detail,
    )
