"""链产物 → 工程装配（M-V8 E3 段3「双轨进工程」）。

把最近一次链运行的音符产物装配成命令层序列（ADR-0017：经 EditBatch 事务落轨）：

- 「<源轨名>」= 03-voice.json（转录原始版）
- 「<源轨名> · 处理」= 05-notes-snapped.json（量化 + 调内吸附后）
- 插在源音频轨正下方（source+1 / source+2）；重跑幂等（先删上次留下的两条同名 MIDI 轨）
- **不 import host**：本模块只产出命令 dict 列表；执行交给 Project.apply_batch
  （webapp/routes/chain.py 组装）——链家族与宿主层零依赖。

幂等语义：删除条件 =「同名（原始名或处理名）且非音频轨」——用户手动建的同名 MIDI 轨
也会被视作旧产物轨（正常不在该场景内；如需保留请改名）。
"""

from __future__ import annotations

import json
from pathlib import Path


def load_run_notes(run_dir: Path) -> dict:
    """读一轮 run 目录的音符产物；返回 {"raw": [...], "processed": [...]}（缺 → None）。

    raw = 03-voice.json（转录原始；兼容 {notes:[...]} 或裸数组）
    processed = 05-notes-snapped.json（处理版）
    """
    def _read(name: str):
        p = Path(run_dir) / name
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        notes = data.get("notes") if isinstance(data, dict) else data
        return notes if isinstance(notes, list) else None

    return {"raw": _read("03-voice.json"), "processed": _read("05-notes-snapped.json")}


def _track_attr(t, key: str, default=None):
    """轨对象 / dict 双兼容取字段（调用方可能传 Track 或 to_dict()）。"""
    if isinstance(t, dict):
        return t.get(key, default)
    return getattr(t, key, default)


def build_apply_commands(score_tracks: list, *, source_track: int,
                         raw_notes: list, processed_notes: list) -> dict:
    """组装「双轨进工程」命令序列（模拟执行，索引全部按「执行到该命令时」的轨道表算出）。

    返回 {"commands": [...], "meta": {...}}；commands 为命令层 dict
    （{op, track?, value?}），按序执行即得两轨进工程。
    """
    names = [str(_track_attr(t, "name", "") or "") for t in score_tracks]
    kinds = [str(_track_attr(t, "kind", "midi") or "midi") for t in score_tracks]
    if not (0 <= source_track < len(names)):
        raise ValueError(f"source_track 越界：{source_track}（共 {len(names)} 轨）")

    source_name = names[source_track] or f"轨道 {source_track + 1}"
    raw_name = source_name
    proc_name = f"{source_name} · 处理"

    # 旧产物轨（同名 MIDI 轨；不含源轨自身）→ 删除（从后往前，索引稳定）
    olds = [i for i, (n, k) in enumerate(zip(names, kinds))
            if k != "audio" and i != source_track and n in (raw_name, proc_name)]

    commands: list[dict] = []
    sim_names = list(names)
    for i in sorted(olds, reverse=True):
        commands.append({"op": "remove_track", "track": i})
        sim_names.pop(i)

    src_idx = source_track - sum(1 for i in olds if i < source_track)
    at1 = src_idx + 1
    at2 = src_idx + 2

    commands.append({"op": "add_track", "track": 0,
                     "value": {"name": raw_name, "at": at1, "unique": False}})
    commands.append({"op": "add_track", "track": 0,
                     "value": {"name": proc_name, "at": at2, "unique": False}})
    commands.append({"op": "set_notes", "track": at1, "value": {"notes": raw_notes}})
    commands.append({"op": "set_notes", "track": at2, "value": {"notes": processed_notes}})

    return {
        "commands": commands,
        "meta": {
            "source_track": source_track,
            "names": [raw_name, proc_name],
            "inserted": [at1, at2],
            "removed": sorted(olds),
            "raw_notes": len(raw_notes or []),
            "processed_notes": len(processed_notes or []),
        },
    }
