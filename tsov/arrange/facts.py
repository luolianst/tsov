"""配器事实底座（M-V8 E4 段3 · Q14 三段一库之③）：结构 / 段型 / 逐小节和弦 / 旋律统计。

decide（LLM 只读本文件选 ID）与 expand（机械展开）的公共输入；**只读 Score、不改工程**。
- 段 = kind="section" 的 project 书签（与 tsov.tune.facts 同口径）
- 拍号泛化：bar_sec = 60/bpm × (4×num/den)，gpb = num×16/den（6/8 → 12 格 / 4/4 → 16 格）
- 段型分类：标签关键词表（pre 优先于 chorus——「预副歌」含「副歌」）；兜底 verse 并标记 source
- 输出全部 JSON 可序列化（chords 转 dict；浮点统一 round）
"""

from __future__ import annotations

import math
import time

from ..core.names import NOTE_NAMES
from ..core.score import parse_time_signature
from . import harmony as _harmony
from .library import PatternLibrary, load_patterns

# 段标签关键词 → 段型（顺序即优先级）
_KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("intro", ("intro", "前奏", "序曲", "prelude", "opening")),
    ("pre", ("pre-chorus", "pre chorus", "prechorus", "预副歌", "导歌")),
    ("chorus", ("chorus", "副歌", "サビ", "hook", "refrain", "高潮")),
    ("bridge", ("bridge", "桥段", "桥")),
    ("interlude", ("interlude", "间奏", "solo", "独奏", "instrumental")),
    ("outro", ("outro", "尾奏", "尾声", "结束")),
    ("verse", ("verse", "主歌", "正歌")),
)


def classify_section(label: str) -> tuple[str, str]:
    """段标签 → (kind, source)；source='label'（关键词命中）/ 'fallback'（默认 verse）。"""
    s = str(label or "").strip().lower()
    for kind, kws in _KIND_RULES:
        for kw in kws:
            if kw in s:
                return kind, "label"
    return "verse", "fallback"


def _meter(score) -> dict:
    num, den = parse_time_signature(getattr(score, "time_signature", None))
    bpm = float(getattr(score, "tempo", 120.0) or 120.0)
    bar_sec = (60.0 / bpm) * (4.0 * num / den)
    gpb = max(1, int(round(num * 16 / den)))
    return {"time_signature": str(getattr(score, "time_signature", "") or "4/4"),
            "tempo": round(bpm, 3), "bar_sec": round(bar_sec, 9), "gpb": gpb,
            "grid_sec": round(bar_sec / gpb, 12)}


def _key_text(score) -> str:
    cands = list(getattr(score, "key_candidates", []) or [])
    if not cands:
        try:
            from ..core.key import detect_key

            notes = [n for t in (getattr(score, "tracks", []) or []) for n in (t.notes or [])]
            cands = detect_key(notes, top=1)
        except Exception:  # noqa: BLE001 —— 检测失败不得中断事实装配
            cands = []
    if not cands:
        return "C major"
    c0 = cands[0]
    key = getattr(c0, "key", None) or (c0.get("key") if isinstance(c0, dict) else None)
    return str(key) if key else "C major"


def _pick_melody(score, melody_track):
    tracks = list(getattr(score, "tracks", []) or [])
    if not tracks:
        raise ValueError("工程没有轨道（无法配器）")
    if melody_track is not None:
        i = int(melody_track)
        if not (0 <= i < len(tracks)):
            raise ValueError(f"melody_track 越界：{melody_track}（共 {len(tracks)} 轨）")
        return i, tracks[i]
    for i, t in enumerate(tracks):
        if str(getattr(t, "name", "") or "").strip().lower() == "melody":
            return i, t
    return 0, tracks[0]


def build_arrange_facts(score, *, pack: str, strength: str = "standard", melody_track=None,
                        name: str | None = None, presets_root=None, ts: float | None = None,
                        library: PatternLibrary | None = None) -> dict:
    """配器事实装配（纯读）：结构 + 段型 + 小节和弦 + 旋律统计 + 模式库目录。"""
    lib = library or load_patterns(pack, root=presets_root)
    if str(strength) not in lib.strengths:
        raise ValueError(f"strength 非法：{strength!r}（可用 {sorted(lib.strengths)}）")
    meter = _meter(score)
    bar_sec, gpb = meter["bar_sec"], meter["gpb"]
    # 拍号一致性护栏（格位时间 = 本谱网格 × 包内格号；错配会产越格音符，拒绝而非「修复」）
    if int(lib.grids_per_bar) != int(gpb):
        raise ValueError(
            f"拍号不匹配：风格包 {lib.pack} 为 {lib.meter}（{lib.grids_per_bar} 格/小节），"
            f"工程为 {meter['time_signature']}（{gpb} 格/小节）——请换与拍号相符的包")
    mi, mtrack = _pick_melody(score, melody_track)
    notes = list(getattr(mtrack, "notes", []) or [])
    if not notes:
        raise ValueError(f"旋律轨无音符：track[{mi}] {str(getattr(mtrack, 'name', '') or '')!r}——先录一条旋律再配器")

    total = float(max(n.end for n in notes))
    n_bars = max(1, int(math.ceil(total / bar_sec - 1e-9)))
    bars = [{"i": b + 1, "start": round(b * bar_sec, 9), "end": round((b + 1) * bar_sec, 9)}
            for b in range(n_bars)]

    # 段（project section 书签）
    raw: list[tuple[str, float, float]] = []
    for bm in (getattr(score, "bookmarks", []) or []):
        if str(getattr(bm, "scope", "")) != "project" or str(getattr(bm, "kind", "")) != "section":
            continue
        if getattr(bm, "end", None) is None:
            continue
        raw.append((str(getattr(bm, "label", "") or ""), float(bm.start), float(bm.end)))
    raw.sort(key=lambda x: x[1])
    synthetic = False
    if not raw:
        # 无段书签 → 合成整曲单段（label=整曲；kind=full 走角色综合默认档）——零配置可用
        raw = [("整曲", 0.0, float(total))]
        synthetic = True
    sections: list[dict] = []
    for idx, (label, s, e) in enumerate(raw):
        e = max(e, s + 1e-6)
        if synthetic:
            kind, src = "full", "synthetic"
        else:
            kind, src = classify_section(label)
        sb = max(1, int(math.floor(s / bar_sec + 1e-9)) + 1)
        eb = min(n_bars, max(sb, int(math.ceil(e / bar_sec - 1e-9))))
        n_sec = sum(1 for n in notes if s <= n.start < e)
        sections.append({"index": idx, "label": label or f"段{idx + 1}", "kind": kind,
                         "kind_source": src, "synthetic": synthetic,
                         "start": round(s, 9), "end": round(e, 9),
                         "start_bar": sb, "end_bar": eb, "bars": eb - sb + 1,
                         "melody_notes": n_sec,
                         "melody_density": round(n_sec / max(e - s, 1e-6), 3)})
    dens_max = max([x["melody_density"] for x in sections], default=0.0) or 1.0
    for x in sections:
        x["energy"] = round(min(1.0, x["melody_density"] / dens_max), 2)

    # 逐小节和弦（harmonize_bars；6/8 由 bar_sec 给出 3 拍边界）
    chords = _harmony.harmonize_bars(notes, _key_text(score), [(b["start"], b["end"]) for b in bars])
    per_bar = []
    for b, c in zip(bars, chords):
        pc = int(c.root_pc) % 12
        per_bar.append({"bar": b["i"], "root_pc": int(c.root_pc), "kind": str(c.kind),
                        "tones": sorted(int(t) % 12 for t in (c.tones or [])),
                        "label": f"{NOTE_NAMES[pc]}{'m' if c.kind == 'minor' else ''}"})

    pitches = [int(n.pitch_midi) for n in notes]
    return {
        "project": str(name or ""),
        "built_at": float(ts if ts is not None else time.time()),
        "pack": lib.pack,
        "strength": str(strength),
        "key": _key_text(score),
        "meter": {**meter, "bars_total": n_bars},
        "duration_sec": round(total, 3),
        "melody": {"track": mi, "name": str(getattr(mtrack, "name", "") or ""),
                   "notes": len(notes), "register": [min(pitches), max(pitches)],
                   "mean_midi": round(sum(pitches) / len(pitches), 2)},
        "bars": bars,
        "sections": sections,
        "chords": {"per_bar": per_bar},
        "roles": lib.catalog(),
        "fills": lib.fill_catalog(),
    }
