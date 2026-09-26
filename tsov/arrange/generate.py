"""配器生成编排（M-V8 E4 段3 · Q14 三段一库）：事实 → 决策（LLM 只选 ID）→ 展开 → 轨规格 → 批次落盘。

- assemble_tracks：计划（每角色 × 每段变体 ID）→ 逐段 expand（坐标全机械）→ 每角色一条轨规格
  （全空角色不出轨；同名轨唯一，采纳侧靠名单幂等）
- generate_batch：建事实 → decide → 装配 → store.save_batch + 记账事件 → 批次 dict
- tracks_from_specs / candidate_score：轨规格 → 真 Track / 候选 Score（试听渲染用，工程零改动）
- 采纳不在这里：build_arrange_commands（apply.py）产命令，routes/arrange.py 走命令层事务
"""

from __future__ import annotations

import time

from ..core.notes import Note
from . import store
from .decide import decide as decide_plan
from .expand import expand_role_section, role_program, role_track_name
from .facts import build_arrange_facts
from .library import PatternLibrary, load_patterns


def assemble_tracks(lib: PatternLibrary, facts: dict, plan: dict, *, melody_notes: list[Note],
                    strength: str | None = None) -> list[dict]:
    """计划 → 轨规格列表（JSON 可序列化；notes 落 Note.to_dict）。"""
    specs: list[dict] = []
    for role in lib.role_ids():
        sec_map = (plan.get("roles") or {}).get(role) or {}
        notes: list[Note] = []
        sec_infos: dict[str, dict] = {}
        for sec in facts["sections"]:
            entry = sec_map.get(str(int(sec["index"])))
            if not isinstance(entry, dict):
                continue
            res = expand_role_section(lib, facts, int(sec["index"]), role, str(entry.get("variant") or ""),
                                      strength=strength, fill=entry.get("fill"), melody_notes=melody_notes)
            if res.notes:
                notes.extend(res.notes)
                sec_infos[str(int(sec["index"]))] = {
                    "n": len(res.notes), "variant": res.variant, "fill": res.fill,
                    "crash": bool(res.crash), "melody_offset": res.melody_offset,
                }
        if not notes:
            continue
        notes.sort(key=lambda n: (n.start, n.pitch_midi))
        spec = lib.role(role)
        try:
            pan = float(spec.get("pan")) if spec.get("pan") is not None else 0.0
        except (TypeError, ValueError):
            pan = 0.0
        specs.append({
            "role": role, "name": role_track_name(lib, role), "program": role_program(lib, role),
            "pan": pan, "n_notes": len(notes), "sections": sec_infos,
            "notes": [n.to_dict() for n in notes],
        })
    return specs


def tracks_from_specs(specs: list[dict]) -> list:
    """轨规格 → 真 Track 对象（候选 Score / 试听渲染用）。"""
    from ..core.score import Instrument, Track

    out = []
    for s in specs:
        notes = [Note.from_dict(d) for d in (s.get("notes") or [])]
        try:
            pan = float(s.get("pan") or 0.0)
        except (TypeError, ValueError):
            pan = 0.0
        out.append(Track(
            name=str(s.get("name") or "配器轨"),
            instrument=Instrument(backend="fluidsynth", program=str(s.get("program") or ""), volume=0.8),
            notes=notes, pan=pan,
        ))
    return out


def candidate_score(score, specs: list[dict]):
    """当前 Score 深拷贝 + 追加配器轨（不落盘、不触碰工程对象）。"""
    from copy import deepcopy

    new = deepcopy(score)
    new.tracks = list(new.tracks) + tracks_from_specs(specs)
    return new


def generate_batch(proj_root, score, *, pack: str, strength: str = "standard", melody_track=None,
                   name: str | None = None, context_md: str = "", key=None, post=None,
                   presets_root=None, facts: dict | None = None, library=None) -> dict:
    """事实 → 决策 → 展开 → 轨规格 → 批次落盘 + 记账（暂存区 producer=arrange）。"""
    lib = library or load_patterns(pack, root=presets_root)
    facts = facts or build_arrange_facts(score, pack=pack, strength=strength, melody_track=melody_track,
                                         name=name, presets_root=presets_root, library=lib)
    plan = decide_plan(facts, key=key, context_md=context_md, post=post, library=lib)
    mi = int(facts["melody"]["track"])
    melody_notes = list(getattr(score.tracks[mi], "notes", []) or [])
    specs = assemble_tracks(lib, facts, plan, melody_notes=melody_notes, strength=str(facts["strength"]))

    ts = store.new_batch_ts(proj_root)
    tracks_meta = [{"role": s["role"], "name": s["name"], "program": s["program"],
                    "n_notes": s["n_notes"], "sections": s["sections"]} for s in specs]
    batch = {
        "batch_ts": ts,
        "project": str(name or ""),
        "pack": lib.pack,
        "strength": str(facts["strength"]),
        "created_at": time.time(),
        "state": "pending",
        "overall": str(plan.get("overall") or ""),
        "plan": plan,
        "tracks": specs,
        "tracks_meta": tracks_meta,
        "facts": facts,
        "stats": {
            "tracks": len(specs),
            "notes": sum(s["n_notes"] for s in specs),
            "source": (plan.get("stats") or {}).get("source"),
            "llm_rows": (plan.get("stats") or {}).get("llm_rows"),
            "dropped": (plan.get("stats") or {}).get("dropped") or [],
            "errors": (plan.get("stats") or {}).get("errors") or [],
            "attempts": (plan.get("stats") or {}).get("attempts"),
        },
    }
    store.save_batch(proj_root, batch)
    store.append_event(proj_root, "generate",
                       {"batch_ts": ts, "pack": lib.pack, "strength": batch["strength"],
                        "tracks": batch["stats"]["tracks"], "notes": batch["stats"]["notes"],
                        "source": batch["stats"]["source"]})
    return batch
