"""音符级 diff（ADR-0015）：每轮 LLM 修改 → 三色可视化的数据源。

对齐协议：按 (start, pitch) 排序后贪心配对（|Δstart| ≤ tol_s 且 pitch 相同）——
- 配对中 end/velocity/deviation_cents 有变化 → changed（黄）
- 未配对的旧音 → removed（红）；未配对的新音 → added（绿）

to_dict() 输出可直接给前端卷帘叠层（docs/05 前端接口契约的数据格式）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..core.notes import Note


@dataclass
class NoteDiff:
    added: list[Note] = field(default_factory=list)
    removed: list[Note] = field(default_factory=list)
    changed: list[tuple[Note, Note]] = field(default_factory=list)  # (旧音, 新音)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)

    def summary(self) -> str:
        parts = [f"+{len(self.added)}", f"-{len(self.removed)}"]
        if self.changed:
            parts.append(f"~{len(self.changed)}")
        return " ".join(parts) if self.total else "无改动"

    def to_dict(self) -> dict:
        return {
            "added": [n.to_dict() for n in self.added],
            "removed": [n.to_dict() for n in self.removed],
            "changed": [{"old": o.to_dict(), "new": n.to_dict()} for o, n in self.changed],
            "summary": self.summary(),
            "total": self.total,
        }


def diff_notes(before: list[Note], after: list[Note], tol_s: float = 0.02) -> NoteDiff:
    """两个音符列表 → NoteDiff（单轨）。"""
    olds = sorted(before, key=lambda n: (n.start, n.pitch_midi))
    news = sorted(after, key=lambda n: (n.start, n.pitch_midi))

    used: set[int] = set()
    pairs: list[tuple[Note, Note]] = []
    for o in olds:
        best: int | None = None
        for j, n in enumerate(news):
            if j in used:
                continue
            if n.pitch_midi == o.pitch_midi and abs(n.start - o.start) <= tol_s:
                if best is None or abs(n.start - o.start) < abs(news[best].start - o.start):
                    best = j
        if best is not None:
            used.add(best)
            pairs.append((o, news[best]))

    diff = NoteDiff()
    for o, n in pairs:
        if (
            abs(o.end - n.end) > 1e-6
            or abs(o.velocity - n.velocity) > 1e-6
            or abs(o.deviation_cents - n.deviation_cents) > 1e-3
        ):
            diff.changed.append((o, n))
    diff.removed = [o for o in olds if all(p[0] is not o for p in pairs)]
    diff.added = [n for j, n in enumerate(news) if j not in used]
    return diff
