"""乐句/段落分割（Voice.segments）。M2 实现，M8 升级。

信号：① 停顿（音符间静音）② 音高突变。M8 修复两个极端：
- 成品歌（GAME）碎片化严重（alison 87 段）→ gap 阈值自适应 + 碎段合并
- 哼唱（RMVPE）分割不足（hum05 全曲 1 段）→ gap 阈值按实际间隔分布自适应（75 分位数）

零额外依赖（numpy 除外），逻辑透明可调参；LLM 语义校正是后续演进。
"""

from __future__ import annotations

import numpy as np

from ..core.notes import Note, Segment


def split_into_segments(
    notes: list[Note],
    gap_ms: float | None = None,
    min_seg_ms: float = 1500.0,
    **params,
) -> list[Segment]:
    """按规则+启发式把音符序列切成乐句/段落。

    - 停顿：间隔 > gap（gap_ms 缺省时取间隔分布 75 分位数，自适应，夹在 [40ms, 800ms]）
    - 音高突变：相邻跳跃 > jump_semitones（默认 7，六度以上）→ 新乐句边界
    - 碎段合并：≤2 个音符的段并入前段（消碎片；短旋律的天然短乐句不会被误并）
    """
    if not notes:
        return []
    notes = sorted(notes, key=lambda n: n.start)
    jump_st = params.get("jump_semitones", 7.0)
    min_note_merge = int(params.get("min_note_merge", 3))  # 音符数 ≤ 此值的段并入前段

    gaps = [notes[i].start - notes[i - 1].end for i in range(1, len(notes))]
    if gap_ms is None:
        if gaps:
            gap_s = float(np.percentile(gaps, 75))
            gap_s = max(0.04, min(0.8, gap_s))  # 自适应 + 边界（紧贴 legato 也能切较大间隔）
        else:
            gap_s = 0.3
    else:
        gap_s = gap_ms / 1000.0

    # 初切：停顿 / 音高突变，记录每段音符数
    spans: list[tuple[float, float, int]] = []
    seg_start = notes[0].start
    seg_count = 1
    for i in range(1, len(notes)):
        prev, cur = notes[i - 1], notes[i]
        gap = cur.start - prev.end
        jump = abs(cur.pitch_midi - prev.pitch_midi)
        if gap > gap_s or jump > jump_st:
            spans.append((seg_start, prev.end, seg_count))
            seg_start = cur.start
            seg_count = 1
        else:
            seg_count += 1
    spans.append((seg_start, notes[-1].end, seg_count))

    # 碎段合并：音符数 ≤ min_note_merge 的段并入前段
    merged: list[tuple[float, float]] = []
    for start, end, count in spans:
        if merged and count <= min_note_merge:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    return [Segment(start=float(s), end=float(e), type="phrase") for s, e in merged]
