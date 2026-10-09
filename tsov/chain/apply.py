"""链产物 → 工程装配（M-V8 E3 段3「进工程」；2026-10-09 起单轨·处理版）。

把最近一次链运行的音符产物装配成命令层序列（ADR-0017：经 EditBatch 事务落轨）：

- 「<源轨名> · 处理」= 05-notes-snapped.json（转录 → 量化 → 调内吸附后的最终版）
  ——10-09 洛怜定：只入最终轨（此前「转录原始 + 处理版」双轨已撤）
- 插在源音频轨正下方（source+1）；重跑幂等（先删上次留下的同名产物轨）
- **不 import host**：本模块只产出命令 dict 列表；执行交给 Project.apply_batch
  （webapp/routes/chain.py 组装）——链家族与宿主层零依赖。

幂等语义：删除条件 =「同名（处理名或旧版原始名）且非音频轨」——旧双轨时代留下的
「<源轨名>」原始轨随重跑一并清退；用户手动建的同名 MIDI 轨也会被视作旧产物轨
（正常不在该场景内；如需保留请改名）。
"""

from __future__ import annotations

import json
from pathlib import Path


def load_run_notes(run_dir: Path) -> dict:
    """读一轮 run 目录的音符产物；返回 {"raw": [...], "processed": [...]}（缺 → None）。

    raw = 03-voice.json（转录原始——10-09 起采纳不再消费，保留为信息/对照用途）
    processed = 05-notes-snapped.json（处理版 = 最终轨；兼容 {notes:[...]} 或裸数组）
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


def build_apply_commands(score_tracks: list, *, source_track: int, notes: list) -> dict:
    """组装「进工程」命令序列（单轨·处理版；索引全部按「执行到该命令时」的轨道表算出）。

    返回 {"commands": [...], "meta": {...}}；commands 为命令层 dict
    （{op, track?, value?}），按序执行即得处理版单轨进工程。
    """
    names = [str(_track_attr(t, "name", "") or "") for t in score_tracks]
    kinds = [str(_track_attr(t, "kind", "midi") or "midi") for t in score_tracks]
    if not (0 <= source_track < len(names)):
        raise ValueError(f"source_track 越界：{source_track}（共 {len(names)} 轨）")

    source_name = names[source_track] or f"轨道 {source_track + 1}"
    proc_name = f"{source_name} · 处理"

    # 旧产物轨（处理名 + 旧双轨时代的原始名；同名的非音频轨、不含源轨自身）
    # → 删除（从后往前，索引稳定）
    olds = [i for i, (n, k) in enumerate(zip(names, kinds))
            if k != "audio" and i != source_track and n in (source_name, proc_name)]

    commands: list[dict] = []
    for i in sorted(olds, reverse=True):
        commands.append({"op": "remove_track", "track": i})

    src_idx = source_track - sum(1 for i in olds if i < source_track)
    at1 = src_idx + 1

    commands.append({"op": "add_track", "track": 0,
                     "value": {"name": proc_name, "at": at1, "unique": False}})
    commands.append({"op": "set_notes", "track": at1, "value": {"notes": notes}})

    return {
        "commands": commands,
        "meta": {
            "source_track": source_track,
            "names": [proc_name],
            "inserted": [at1],
            "removed": sorted(olds),
            "notes": len(notes or []),
        },
    }
