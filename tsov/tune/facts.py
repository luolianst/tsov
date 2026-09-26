"""事实包（M-V8 E4 段2 · Q2）：调参决策的唯一事实底座。

内容 = 结构摘要 + 电平（全曲 + 逐段）+ 粗频谱 + 目标（风格包 level_hint / 通用平衡口径）。

- 范围：全曲 + 逐段（段 = project 作用域 kind="section" 书签）
- 目标解析：track 绑 pack instrument —— ① gm_program 精确匹配 ② 名称（中/英关键词）匹配；
  绑不上 → 该轨无目标（确定性通道跳过该轨）
- 无 pack → 通用平衡口径（关键词 → 通用相对档位）；同样只覆盖绑得上的轨
- 渲染器可注入（测试用合成缓冲）；线上默认 HostEngine + StemStore 缓存（只推子/声像变化不重渲）
- 输出全部 JSON 可序列化（numpy → float；统一 round）

红线（Q5）：本模块只产事实——不产建议、不改工程。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import measure, spectrum

DEFAULT_PRESETS_DIR = Path(__file__).resolve().parents[2] / "presets" / "arrangements"

# 名称关键词 → 角色族（zh + en；与 6 风格包 role 命名的粗映射）
_NAME_ALIASES: tuple[tuple[str, str], ...] = (
    ("鼓", "drum"), ("drum", "drum"), ("kick", "drum"), ("perc", "drum"),
    ("贝斯", "bass"), ("贝司", "bass"), ("bass", "bass"), ("低音", "bass"),
    ("吉他", "guitar"), ("guitar", "guitar"),
    ("钢琴", "piano"), ("piano", "piano"), ("keys", "piano"), ("rhodes", "piano"), ("键", "piano"),
    ("弦乐", "strings"), ("strings", "strings"),
    ("pad", "pad"), ("铺底", "pad"),
    ("长笛", "flute"), ("flute", "flute"),
    ("单簧管", "clarinet"), ("clarinet", "clarinet"),
    ("圆号", "horn"), ("horn", "horn"),
    ("小号", "trumpet"), ("trumpet", "trumpet"),
    ("竖琴", "harp"), ("harp", "harp"),
    ("定音鼓", "timpani"), ("timpani", "timpani"),
    ("风琴", "organ"), ("organ", "organ"),
    ("琶音", "arp"), ("arp", "arp"), ("pluck", "arp"),
    ("lead", "lead"), ("主音", "lead"), ("hook", "lead"), ("旋律", "lead"), ("melody", "lead"),
)

# 通用平衡口径（无 pack 时的相对档位；锚=绑定的 0 dB 档或最响轨）
_GENERIC_HINTS: tuple[tuple[str, str, float], ...] = (
    ("drum", "鼓组", 0.0),
    ("bass", "贝斯", -4.0),
    ("lead", "主奏", -4.0),
    ("piano", "键盘", -6.0),
    ("guitar", "吉他", -9.0),
    ("arp", "琶音", -13.0),
    ("strings", "弦乐", -16.0),
    ("pad", "Pad", -16.0),
)


# ---------------------------------------------------------------------------
# 目标解析
# ---------------------------------------------------------------------------


def track_families(name: str) -> set[str]:
    """轨名 → 角色族集合（关键词粗匹配）。"""
    n = str(name or "").lower()
    fams = set()
    for kw, fam in _NAME_ALIASES:
        if kw in n:
            fams.add(fam)
    return fams


def _role_matches(fams: set[str], role: str) -> bool:
    parts = str(role or "").lower().split("_")
    return any(fam == p or fam in p for fam in fams for p in parts)


def _bind_track(track, pack_roles: list[dict]) -> dict | None:
    """单轨绑 pack 角色：gm_program 精确 → 名称关键词；绑不上 → None。"""
    prog = str(getattr(getattr(track, "instrument", None), "program", "") or "").strip()
    pnum = int(prog) if prog.isdigit() else None
    if pnum is not None:
        for r in pack_roles:
            g = r.get("gm_program")
            if g is not None and int(g) == pnum:
                return {"role": str(r["role"]), "title": str(r.get("title") or ""),
                        "level_hint_db": float(r.get("level_hint_db") or 0.0),
                        "pan": float(r.get("pan") or 0.0), "via": "gm"}
    fams = track_families(getattr(track, "name", ""))
    if fams:
        for r in pack_roles:
            if _role_matches(fams, str(r.get("role") or "")):
                return {"role": str(r["role"]), "title": str(r.get("title") or ""),
                        "level_hint_db": float(r.get("level_hint_db") or 0.0),
                        "pan": float(r.get("pan") or 0.0), "via": "name"}
    return None


def _generic_binding(track) -> dict | None:
    fams = track_families(getattr(track, "name", ""))
    if not fams:
        return None
    for fam, title, lvl in _GENERIC_HINTS:
        if fam in fams:
            return {"role": f"generic:{fam}", "title": title, "level_hint_db": lvl,
                    "pan": None, "via": "generic"}
    return None


def resolve_targets(score, pack=None, *, presets_root=None) -> dict:
    """目标解析 → {source, pack?, title?, mix_notes?, per_track: {i: 绑定}, unmatched}。"""
    tracks = list(getattr(score, "tracks", []) or [])
    if pack:
        path = Path(presets_root or DEFAULT_PRESETS_DIR) / f"{pack}.json"
        if not path.is_file():
            raise ValueError(f"风格包不存在：{pack}（找 {path}）")
        data = json.loads(path.read_text(encoding="utf-8"))
        roles = [r for r in (data.get("instruments") or []) if isinstance(r, dict) and r.get("role")]
        per: dict[str, dict] = {}
        unmatched: list[int] = []
        for i, t in enumerate(tracks):
            b = _bind_track(t, roles)
            if b:
                per[str(i)] = b
            else:
                unmatched.append(i)
        return {"source": f"pack:{pack}", "pack": pack, "title": str(data.get("title") or pack),
                "mix_notes": str(data.get("mix_notes") or "")[:2000],
                "per_track": per, "unmatched": unmatched}
    per, unmatched = {}, []
    for i, t in enumerate(tracks):
        b = _generic_binding(t)
        if b:
            per[str(i)] = b
        else:
            unmatched.append(i)
    return {"source": "generic", "per_track": per, "unmatched": unmatched}


def target_relatives(facts: dict) -> tuple[dict[int, float], int | None]:
    """目标电平换算到与 levels.rel_db 同极（相对同一「极轨」）：({track: 目标相对 dB}, 极轨)。

    极轨优先序：①有 0 dB 档目标的绑定轨（风格包的天然锚）→ ②测量最响且绑定的轨 → ③绑定轨首个。
    无目标 / 无绑定 / 无有效测量 → ({}, None)。
    """
    bind = {int(k): v for k, v in ((facts.get("targets") or {}).get("per_track") or {}).items()}
    if not bind:
        return {}, None
    levels = facts.get("levels") or {}
    rows = {int(r["index"]): r for r in (levels.get("tracks") or [])}
    cands = [i for i in sorted(bind) if i in rows and rows[i].get("rel_db") is not None and not rows[i].get("silent")]
    if not cands:
        return {}, None
    pole = None
    for i in cands:
        if abs(float(bind[i].get("level_hint_db") or 0.0)) < 1e-9:
            pole = i
            break
    loud = levels.get("anchor")
    if pole is None and loud is not None and int(loud) in cands:
        pole = int(loud)
    if pole is None:
        pole = cands[0]
    a_hint = float(bind[pole].get("level_hint_db") or 0.0)
    out = {i: round(float(bind[i].get("level_hint_db") or 0.0) - a_hint, 2) for i in cands}
    return out, pole


def measured_relative(facts: dict, track: int, pole: int | None) -> float | None:
    """测量相对电平（与 target_relatives 同极）：rel_i − rel_pole。"""
    if pole is None:
        return None
    rows = {int(r["index"]): r for r in ((facts.get("levels") or {}).get("tracks") or [])}
    a, b = rows.get(int(track)), rows.get(int(pole))
    if not a or not b or a.get("rel_db") is None or b.get("rel_db") is None:
        return None
    return round(float(a["rel_db"]) - float(b["rel_db"]), 2)


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------


def _key_text(score) -> str:
    cands = list(getattr(score, "key_candidates", []) or [])
    if not cands:
        try:
            from ..core.key import detect_key

            notes = [n for t in (getattr(score, "tracks", []) or []) for n in (t.notes or [])]
            cands = detect_key(notes, top=1)
        except Exception:  # noqa: BLE001 —— 摘要生成不得因检测失败而中断
            cands = []
    if not cands:
        return "—"
    c0 = cands[0]
    key = getattr(c0, "key", None) or (c0.get("key") if isinstance(c0, dict) else None)
    return str(key) if key else "—"


def _structure(score, *, samplerate: int) -> dict:
    tracks = []
    for i, t in enumerate(score.tracks):
        notes = list(getattr(t, "notes", []) or [])
        pitches = [int(n.pitch_midi) for n in notes]
        inst = getattr(t, "instrument", None)
        effects = []
        for k, fx in enumerate(getattr(inst, "effects", []) or []):
            effects.append({"index": k, "type": str(getattr(fx, "type", "")),
                            "params": dict(getattr(fx, "params", {}) or {})})
        tracks.append({
            "index": i,
            "name": str(getattr(t, "name", "") or f"轨道 {i + 1}"),
            "kind": str(getattr(t, "kind", "midi") or "midi"),
            "notes": len(notes),
            "register": [min(pitches), max(pitches)] if pitches else None,
            "program": str(getattr(inst, "program", "") or ""),
            "volume": round(float(getattr(inst, "volume", 1.0) or 0.0), 4),
            "pan": round(float(getattr(t, "pan", 0.0) or 0.0), 4),
            "mute": bool(getattr(t, "mute", False)),
            "solo": bool(getattr(t, "solo", False)),
            "effects": effects,
            "families": sorted(track_families(getattr(t, "name", ""))),
        })
    sections = []
    for bm in (getattr(score, "bookmarks", []) or []):
        if str(getattr(bm, "scope", "")) != "project" or str(getattr(bm, "kind", "")) != "section":
            continue
        if getattr(bm, "end", None) is None:
            continue
        sections.append({"label": str(getattr(bm, "label", "") or f"段{len(sections) + 1}"),
                         "start": round(float(bm.start), 2), "end": round(float(bm.end), 2)})
    sections.sort(key=lambda s: s["start"])
    ends = [n.end for t in score.tracks for n in (getattr(t, "notes", []) or [])]
    return {
        "tempo": round(float(getattr(score, "tempo", 120.0) or 120.0), 2),
        "time_signature": str(getattr(score, "time_signature", "") or "4/4"),
        "key": _key_text(score),
        "duration_sec": round(max(ends), 2) if ends else 0.0,
        "notes_total": sum(len(getattr(t, "notes", []) or []) for t in score.tracks),
        "samplerate": int(samplerate),
        "tracks": tracks,
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# 默认渲染器（线上）
# ---------------------------------------------------------------------------


class EngineRenderer:
    """线上渲染器：HostEngine 会话 + StemStore 缓存（ADR-0018：只推子/声像变化不重渲）。"""

    def __init__(self, root, score, cache=None):
        from ..host import HostEngine
        from ..host.cache import StemStore
        from ..host.mix import render_buses

        self._render = render_buses
        self._engine = HostEngine()
        self.session = self._engine.load(score, base_dir=root)
        self.samplerate = int(self.session.samplerate)
        self._cache = cache if cache is not None else StemStore(root)

    def track(self, i: int):
        return self._render(self.session, self.samplerate, stereo=False, only_track=int(i),
                            include_bus_processing=False, include_master_processing=False,
                            auto_scale=False, cache=self._cache)

    def mix(self):
        return self._render(self.session, self.samplerate, stereo=False, auto_scale=False,
                            cache=self._cache)

    def close(self) -> None:
        self.session.close()


# ---------------------------------------------------------------------------
# 事实装配
# ---------------------------------------------------------------------------


def _empty_level_row(i: int, name: str) -> dict:
    return {"index": i, "name": name, "silent": True, "duration_sec": 0.0,
            "rms_dbfs": None, "loudest_win_dbfs": None, "loudest_win_at": None,
            "peak_dbfs": None, "rel_db": None}


def build_facts(proj_root, score, *, name: str | None = None, pack: str | None = None,
                window_sec: float = 4.0, renderer=None, presets_root=None, cache=None,
                ts: float | None = None) -> dict:
    """事实装配（一遍渲染循环：每轨渲染一次，指标/频谱/逐段全部落数后即弃缓冲）。

    renderer 注入须实现 track(i)/mix()/samplerate/close()（见 EngineRenderer）。
    """
    root = Path(proj_root)
    own = renderer is None
    r = renderer or EngineRenderer(root, score, cache=cache)
    try:
        sr = int(r.samplerate)
        structure = _structure(score, samplerate=sr)
        sections = structure["sections"]
        mix_db, clip = measure.mix_peak(r.mix())

        level_rows: list[dict] = []
        spec_rows: list[dict] = []
        sec_rows: list[dict] = []
        for i, t in enumerate(score.tracks):
            nm = str(getattr(t, "name", "") or f"轨道 {i + 1}")
            has_content = bool(getattr(t, "notes", []) or []) or str(getattr(t, "kind", "")) == "audio"
            if not has_content:
                level_rows.append(_empty_level_row(i, nm))
                spec_rows.append({"index": i, "name": nm, "bands": [0.0] * len(spectrum.BANDS),
                                  "centroid_hz": 0.0})
                continue
            buf = r.track(i)
            m = measure.track_metrics(buf, sr, window_sec=window_sec)
            level_rows.append({"index": i, "name": nm, **m, "rel_db": None})
            spec_rows.append({"index": i, "name": nm, "bands": spectrum.band_shares(buf, sr),
                              "centroid_hz": spectrum.spectral_centroid(buf, sr)})
            for sec in sections:
                sec_rows.append({"track": i, "label": sec["label"],
                                 "rms_dbfs": measure.section_rms_dbfs(buf, sr, sec["start"], sec["end"])})

        loud = [x for x in level_rows if x.get("rms_dbfs") is not None and not x.get("silent")]
        anchor = None
        if loud:
            anchor = int(max(loud, key=lambda x: x["rms_dbfs"])["index"])
            ref = next(float(x["rms_dbfs"]) for x in loud if x["index"] == anchor)
            for x in loud:
                x["rel_db"] = round(float(x["rms_dbfs"]) - ref, 2)

        return {
            "project": str(name or root.name),
            "built_at": float(ts if ts is not None else time.time()),
            "pack": pack,
            "structure": structure,
            "levels": {"window_sec": round(float(window_sec), 2), "mix_peak_dbfs": mix_db,
                       "clipping": bool(clip), "anchor": anchor, "tracks": level_rows},
            "spectrum": {"bands": [{"lo": lo, "hi": hi, "label": lab} for lo, hi, lab in spectrum.BANDS],
                         "tracks": spec_rows},
            "sections_levels": sec_rows,
            "targets": resolve_targets(score, pack, presets_root=presets_root),
        }
    finally:
        if own:
            r.close()
