"""语义层生成：固定模板 + 规则填充（ADR-0005 决策 5）。

纯规则、确定性、无 LLM 参与——LLM 只负责"读 + 分析"。保证 M2 双底座对比公平
（同样输入同样模板，差异只来自底座）。M3 填充实现。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..core.notes import Voice

# 音名表：MIDI 音高 → 音名（midi % 12），八度 = midi // 12 - 1（midi 60 = C4）
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

CONFIDENCE_LABELS = {
    "high": "高（可靠）",
    "medium": "中（存疑）",
    "low": "低（不可靠）",
}


def midi_to_note_name(pitch_midi: int) -> str:
    """MIDI 音高 → 音名 + 八度（如 60 → C4，69 → A4）。"""
    pitch_midi = int(pitch_midi)
    name = NOTE_NAMES[pitch_midi % 12]
    octave = pitch_midi // 12 - 1
    return f"{name}{octave}"


def confidence_label(confidence: float, high: float = 0.7, medium: float = 0.4) -> str:
    """置信度 → 高/中/低标签（阈值可调，代码内默认值）。"""
    if confidence >= high:
        return "high"
    if confidence >= medium:
        return "medium"
    return "low"


def build_semantic_dataset(voice: Voice, **params) -> dict[str, Any]:
    """原始层 Voice → 语义层中间数据集（人读/LLM 读）。

    - 音符转音名 / 相对时值 / 乐句分组
    - 置信度转"高/中/低"标签
    - 附 DSP 特征与局限说明段（供 prompt 引用）
    - 纯规则填充，无 LLM 参与（ADR-0005 决策 5）
    """
    if voice is None:
        raise TypeError("voice 不能为 None")

    notes = sorted(voice.notes, key=lambda n: n.start)
    beat_s = 60.0 / voice.bpm if voice.bpm > 0 else 0.5
    segments = list(voice.segments)

    def phrase_index(note) -> int:
        for i, seg in enumerate(segments):
            if seg.start - 1e-6 <= note.start <= seg.end + 1e-6:
                return i + 1
        if segments:
            return len(segments) + 1
        return 1

    semantic_notes: list[dict[str, Any]] = []
    for idx, n in enumerate(notes):
        duration_beats = round((n.end - n.start) / beat_s, 3)
        semantic_notes.append(
            {
                "index": idx,
                "note_name": midi_to_note_name(n.pitch_midi),
                "pitch_midi": int(n.pitch_midi),
                "pitch_hz": round(float(n.pitch_hz), 2),
                "start_sec": round(float(n.start), 3),
                "end_sec": round(float(n.end), 3),
                "relative_duration_beats": duration_beats,
                "velocity": round(float(n.velocity), 3),
                "confidence": round(float(n.confidence), 3),
                "confidence_label": confidence_label(n.confidence, **params),
                "deviation_cents": round(float(n.deviation_cents), 1),
                "is_ornament": bool(n.is_ornament),
                "phrase": phrase_index(n),
            }
        )

    deviations = [abs(float(n.deviation_cents)) for n in notes if n.deviation_cents]
    duration_total = float(notes[-1].end - notes[0].start) if notes else 0.0
    deviation_stats = {
        "count_abs_gt_25c": int(sum(1 for d in deviations if d > 25)),
        "mean_abs_cents": round(float(np.mean(deviations)), 1) if deviations else 0.0,
        "max_abs_cents": round(float(np.max(deviations)), 1) if deviations else 0.0,
    }

    phrase_grouping = [
        {
            "phrase": i + 1,
            "start_sec": round(float(seg.start), 3),
            "end_sec": round(float(seg.end), 3),
            "note_count": sum(1 for s in semantic_notes if s["phrase"] == i + 1),
        }
        for i, seg in enumerate(segments)
    ]
    if not phrase_grouping and notes:
        phrase_grouping = [
            {
                "phrase": 1,
                "start_sec": round(float(notes[0].start), 3),
                "end_sec": round(float(notes[-1].end), 3),
                "note_count": len(notes),
            }
        ]

    dsp_features = {
        "backend": voice.backend,
        "note_count": len(notes),
        "duration_sec": round(duration_total, 3),
        "bpm": voice.bpm,
        "bpm_confidence": round(float(voice.bpm_confidence), 3),
        "segment_count": len(segments),
        "deviation_cents_distribution": deviation_stats,
        "confident_note_ratio": (
            round(float(sum(1 for s in semantic_notes if s["confidence"] >= 0.7) / len(semantic_notes)), 3)
            if semantic_notes
            else 0.0
        ),
    }

    return {
        "schema_version": 1,
        "layer": "semantic",
        "source_audio": voice.source_audio,
        "dsp_features": dsp_features,
        "notes": semantic_notes,
        "phrase_grouping": phrase_grouping,
    }
