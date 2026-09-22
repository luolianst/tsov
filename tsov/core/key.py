"""调性检测（scale-fit）——key_candidates 重算的共用实现。

2026-09-22 架构治理（F1）：自 tsov/analysis/key.py 下沉——纯工具（依赖仅 core）归底部，
消除 host → analysis 反向依赖（原文件随之删除）。

来源：M8 缺口 6（edit_score 改谱后调性同步），实现原在 analysis/edit.py 的 `_detect_key`。
M-V2.4（审计修复）：上移为独立模块——命令层（host/project.apply_batch）与编辑层
（analysis/edit.edit_score）共用同一实现，避免两条路径调性标签不同步
（实测 bug：/batch transpose 后 UI 调性标签陈旧）。

scale-fit：音阶内音占比 + 主音权重——对短旋律/调式（如 D dorian）比 Krumhansl 相关更稳。
"""

from __future__ import annotations

import numpy as np

from .notes import Note
from .score import KeyCandidate

# 调式音阶（相对根音 pitch class）
KEY_SCALES = {
    "major": {0, 2, 4, 5, 7, 9, 11},
    "minor": {0, 2, 3, 5, 7, 8, 10},
    "dorian": {0, 2, 3, 5, 7, 9, 10},
}
_NOTE_NAMES12 = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def detect_key(notes: list[Note], top: int = 2) -> list[KeyCandidate]:
    """按音符 pitch class 直方图重算调性（scale-fit：音阶内音占比 + 主音权重）。

    音符数 < 4 → 返回 []（样本不足不给结论）。
    """
    if len(notes) < 4:
        return []
    hist = np.zeros(12)
    for n in notes:
        hist[int(n.pitch_midi) % 12] += 1.0
    total = float(hist.sum()) or 1.0
    h = hist / total

    scored: list[tuple[float, int, str]] = []
    for root in range(12):
        for mode, scale in KEY_SCALES.items():
            scale_abs = {(s + root) % 12 for s in scale}
            ratio = float(sum(h[pc] for pc in scale_abs))
            tonic_w = float(h[root])
            score = ratio + 0.4 * tonic_w  # 音阶内占比为主，主音出现加分
            scored.append((score, root, f"{_NOTE_NAMES12[root]} {mode}"))

    scored.sort(reverse=True)
    seen_roots: set[int] = set()
    cands: list[KeyCandidate] = []
    for score, root, name in scored:
        if root in seen_roots:
            continue
        seen_roots.add(root)
        conf = round(float(np.clip(score / 1.4, 0.1, 1.0)), 2)  # 归一置信度
        cands.append(KeyCandidate(key=name, confidence=conf))
        if len(cands) >= top:
            break
    return cands
