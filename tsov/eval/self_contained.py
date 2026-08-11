"""自足评估指标（不依赖外部真值）——M2 实现（ADR-0006 接口冻结）。

先跑通"无标准的准确率"（哼唱素材无 ground truth），再上参考谱对比。
指标：音准偏差（平均 cents + 分布）/ 时值误差（onset 对齐）/ 置信度分布 / 音符数合理性。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..core.notes import Voice


@dataclass
class SelfContainedMetrics:
    pitch_accuracy: dict = field(default_factory=dict)  # 音准：平均音分偏移 + 分布（std/p95）
    timing_error: dict = field(default_factory=dict)  # 时值：onset 对齐误差（秒/相对）
    confidence: dict = field(default_factory=dict)  # 置信度：平均 + 低置信度音符占比
    note_count: dict = field(default_factory=dict)  # 音符数合理性（实际值 / 预期区间）
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def compute_self_contained(voice: Voice, **params) -> SelfContainedMetrics:
    """自足指标：音准偏差（平均 cents + 分布）/ 时值误差（onset 对齐）/ 置信度分布 / 音符数合理性。

    - 音准：以 Note.deviation_cents 为原始层保留的"相对量化网格偏移"，取其均值/标准差/p95
    - 时值：相邻音符 onset 间隔的对齐一致性（中位数间隔 + 变异系数）；低置信度处视为"节拍不稳"
    - 置信度：平均置信度 + 低置信度（<0.5）音符占比
    - 音符数：实际音符数 vs 按时长估算的预期区间（假定 1-3 音符/秒）
    """
    notes = voice.notes
    if not notes:
        empty = dict(n=0)
        return SelfContainedMetrics(
            pitch_accuracy={"mean_cents": 0.0, "std_cents": 0.0, "p95_cents": 0.0, "n": 0},
            timing_error={"median_ioi_s": 0.0, "ioi_cv": 0.0, "n": 0},
            confidence={"mean": 0.0, "low_conf_ratio": 0.0, "n": 0},
            note_count=empty,
        )

    notes = sorted(notes, key=lambda n: n.start)

    # 1. 音准偏差（cents）
    devs = np.array([n.deviation_cents for n in notes], dtype=np.float64)
    pitch_accuracy = {
        "mean_cents": round(float(np.mean(devs)), 1),
        "std_cents": round(float(np.std(devs)), 1),
        "p95_cents": round(float(np.percentile(np.abs(devs), 95)), 1),
        "n": len(notes),
    }

    # 2. 时值误差（onset 对齐）：相邻 onset 间隔一致性
    onsets = np.array([n.start for n in notes], dtype=np.float64)
    iois = np.diff(onsets)
    if len(iois) > 0 and iois.size > 1:
        median_ioi = float(np.median(iois))
        ioi_cv = float(np.std(iois) / median_ioi) if median_ioi > 1e-6 else 0.0
    else:
        median_ioi = float(iois[0]) if len(iois) == 1 else 0.0
        ioi_cv = 0.0
    timing_error = {
        "median_ioi_s": round(median_ioi, 3),
        "ioi_cv": round(ioi_cv, 3),  # 变异系数越小越稳定
        "n": len(iois),
    }

    # 3. 置信度分布
    confs = np.array([n.confidence for n in notes], dtype=np.float64)
    confidence = {
        "mean": round(float(np.mean(confs)), 3),
        "low_conf_ratio": round(float(np.mean(confs < 0.5)), 3),
        "n": len(notes),
    }

    # 4. 音符数合理性：实际 vs 预期区间（1-3 音符/秒 × 音频时长）
    duration = notes[-1].end - notes[0].start
    lo = int(duration * 1.0)
    hi = int(duration * 3.0)
    note_count = {
        "actual": len(notes),
        "expected_range": [lo, hi],
        "in_range": lo <= len(notes) <= hi,
        "duration_s": round(duration, 2),
    }

    extra = {"params": {**params}}
    return SelfContainedMetrics(
        pitch_accuracy=pitch_accuracy,
        timing_error=timing_error,
        confidence=confidence,
        note_count=note_count,
        extra=extra,
    )
