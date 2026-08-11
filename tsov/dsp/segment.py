"""乐句/段落分割（Voice.segments）。M2 实现（ADR-0006 定案：规则+启发式）。

信号：① 停顿（音符间静音 >N ms）② 置信度低谷 ③ 音高突变。
零额外依赖，逻辑透明可调参；LLM 语义校正是后续演进（第一版 LLM 只读）。
"""

from __future__ import annotations

from ..core.notes import Note, Segment


def split_into_segments(notes: list[Note], gap_ms: float = 300.0, **params) -> list[Segment]:
    """按规则+启发式把音符序列切成乐句/段落。

    - 停顿：某音符 end 到下一音符 start 的间隔 > gap_ms → 切分
    - 音高突变：相邻音符跳跃 > jump_semitones（如 7 半音，六度以上）→ 视为新乐句边界
    - 输出 Segment {start, end, type} 列表
    """
    if not notes:
        return []
    notes = sorted(notes, key=lambda n: n.start)
    jump_st = params.get("jump_semitones", 7.0)
    gap_s = gap_ms / 1000.0

    segments: list[Segment] = []
    seg_start = notes[0].start
    for i in range(1, len(notes)):
        prev, cur = notes[i - 1], notes[i]
        gap = cur.start - prev.end
        jump = abs(cur.pitch_midi - prev.pitch_midi)
        if gap > gap_s or jump > jump_st:
            segments.append(Segment(start=float(seg_start), end=float(prev.end), type="phrase"))
            seg_start = cur.start
    segments.append(Segment(start=float(seg_start), end=float(notes[-1].end), type="phrase"))
    return segments
