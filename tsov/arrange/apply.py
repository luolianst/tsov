"""配器采纳：轨规格 → 命令层序列（M-V8 E4 段3 · ADR-0017：经 EditBatch 事务落轨）。

- 幂等语义：先删「同名且非音频」的旧产物轨（本次要写的轨名集合；重跑覆盖、不留重复）
- 插入位置：锚轨（旋律）正下方起依次排开（at 逐步 +1）
- 每轨命令序：add_track → set_instrument（GM 名）→ set_notes → set_track_mix（pan 非零才发）
- **不 import host**：本模块只产出命令 dict 列表；执行交给 Project.apply_batch
  （webapp/routes/arrange.py 组装）——配器域与宿主层零依赖（同 chain/apply.py 先例）。
"""

from __future__ import annotations


def _attr(t, key: str, default=None):
    """轨对象 / dict 双兼容取字段。"""
    if isinstance(t, dict):
        return t.get(key, default)
    return getattr(t, key, default)


def build_arrange_commands(score_tracks: list, *, specs: list[dict], anchor_track: int) -> dict:
    """组装「配器轨道进工程」命令序列（索引按「执行到该命令时」的轨道表算出）。

    specs = [{role, name, program, pan, notes: [...], n_notes, sections}]（generate.assemble_tracks 产物）。
    返回 {"commands": [...], "meta": {...}}；按序执行即得多轨进工程。
    """
    if not specs:
        raise ValueError("没有可落地的轨（specs 为空）")
    names = [str(_attr(t, "name", "") or "") for t in score_tracks]
    kinds = [str(_attr(t, "kind", "midi") or "midi") for t in score_tracks]
    if not (0 <= int(anchor_track) < len(names)):
        raise ValueError(f"anchor_track 越界：{anchor_track}（共 {len(names)} 轨）")
    anchor_track = int(anchor_track)

    want = [str(s.get("name") or "") for s in specs]
    dup = len(set(want)) != len(want)
    if dup:
        raise ValueError(f"轨名重复：{want}")

    # 旧产物轨（同名、非音频、不含锚轨自身）→ 删除（从后往前，索引稳定）
    olds = [i for i, (n, k) in enumerate(zip(names, kinds))
            if k != "audio" and i != anchor_track and n in set(want)]

    commands: list[dict] = []
    for i in sorted(olds, reverse=True):
        commands.append({"op": "remove_track", "track": i})

    anchor_idx = anchor_track - sum(1 for i in olds if i < anchor_track)
    at = anchor_idx + 1
    meta_tracks: list[dict] = []
    for s in specs:
        commands.append({"op": "add_track", "track": 0,
                         "value": {"name": str(s["name"]), "at": at, "unique": False}})
        prog = s.get("program")
        if prog:
            commands.append({"op": "set_instrument", "track": at,
                             "value": {"program": str(prog)}})
        commands.append({"op": "set_notes", "track": at, "value": {"notes": list(s.get("notes") or [])}})
        pan = s.get("pan")
        try:
            panf = float(pan) if pan is not None else 0.0
        except (TypeError, ValueError):
            panf = 0.0
        if abs(panf) > 1e-9:
            commands.append({"op": "set_track_mix", "track": at,
                             "value": {"pan": round(panf, 4)}})
        meta_tracks.append({"name": str(s["name"]), "at": at, "role": s.get("role"),
                            "program": prog, "notes": len(s.get("notes") or [])})
        at += 1

    return {
        "commands": commands,
        "meta": {
            "anchor_track": anchor_track,
            "removed": sorted(olds),
            "tracks": meta_tracks,
            "n_tracks": len(meta_tracks),
            "n_notes": sum(t["notes"] for t in meta_tracks),
        },
    }
