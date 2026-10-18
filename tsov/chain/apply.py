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


def _source_audio_start(t) -> float:
    """源轨若为音频轨：取首个 clip 的时间线起点；无 clip/非音频 → 0。"""
    if str(_track_attr(t, "kind", "midi") or "midi") != "audio":
        return 0.0
    getter = getattr(t, "audio_clips", None)
    if callable(getter):
        clips = getter()
    else:
        from ..core.score import normalize_audio_clips
        clips = normalize_audio_clips(_track_attr(t, "audio", None))
    return float(clips[0].get("start") or 0.0) if clips else 0.0


def build_apply_commands(score_tracks: list, *, source_track: int, notes: list,
                         mount: dict | None = None) -> dict:
    """组装「进工程」命令序列（单轨·处理版；索引全部按「执行到该命令时」的轨道表算出）。

    返回 {"commands": [...], "meta": {...}}；commands 为命令层 dict
    （{op, track?, value?}），按序执行即得处理版单轨进工程。
    S4.2：``mount={"source_id": ...}`` 时在产轨后追加 ``mount_source``（快车道 apply.mount）。
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
    source_start = _source_audio_start(score_tracks[source_track])
    if source_start > 0:
        # 转录落点 = 源音频 clip 起点（Part 整体平移；音符保持链产物绝对时间）
        commands.append({"op": "move_part", "track": at1,
                         "value": {"part": 0, "start": round(source_start, 6)}})
    mount = mount if isinstance(mount, dict) else {}
    mount_sid = str(mount.get("source_id") or "").strip()
    if mount_sid:
        commands.append({"op": "mount_source", "track": at1,
                         "value": {"source_id": mount_sid}})

    return {
        "commands": commands,
        "meta": {
            "source_track": source_track,
            "names": [proc_name],
            "inserted": [at1],
            "removed": sorted(olds),
            "notes": len(notes or []),
            "mount_source": mount_sid,
        },
    }


# ---------------------------------------------------------------------------
# 批E 段1（E1-5）：拆解落位——文件夹「拆解 · <源轨名>」＋音频轨×4＋MIDI 轨×N
# ---------------------------------------------------------------------------

_SEP_ORDER = ("vocals", "drums", "bass", "other")
_SEP_AUDIO_NAMES = {"vocals": "人声", "drums": "鼓", "bass": "贝斯", "other": "其他"}
_SEP_MIDI_NAMES = {"vocals": "人声 MIDI", "drums": "鼓 MIDI", "bass": "贝斯 MIDI", "other": "其他 MIDI"}


def load_decompose_artifacts(run_dir: Path) -> dict:
    """读拆解 run 的落位素材：四 stems 音频 ＋ **全部在案声部**的音符（段2 起）。

    - stems：{声部: "stems/<声部>.wav" or None}（相对 run_dir；None=缺件）
    - notes：{声部: [音符]}——每声部按 notes-<声部>-snapped → -quantized → voice-<声部>
      取首个在案（量化/吸附被 mute 时自动回退到未量化版——卡片可关语义）；
      无转录产物的声部不入表（链没跑到的声部不落空轨）
    - sources：{声部: 文件名}（对照/审计用）
    - vocals_notes / notes_source：兼容键（＝ vocals 项；E1 版消费方仍可读）
    """
    stems: dict = {}
    for s in _SEP_ORDER:
        p = Path(run_dir) / "stems" / f"{s}.wav"
        stems[s] = f"stems/{s}.wav" if p.is_file() else None
    notes_by_stem: dict = {}
    sources: dict = {}
    for s in _SEP_ORDER:
        for name in (f"notes-{s}-snapped.json", f"notes-{s}-quantized.json", f"voice-{s}.json"):
            p = Path(run_dir) / name
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            notes = data.get("notes") if isinstance(data, dict) else data
            if isinstance(notes, list):
                notes_by_stem[s] = notes
                sources[s] = name
                break
    return {"stems": stems, "notes": notes_by_stem, "sources": sources,
            "vocals_notes": notes_by_stem.get("vocals"), "notes_source": sources.get("vocals")}


def build_decompose_commands(score_tracks: list, *, source_track: int, stems: dict,
                             midis: list[dict]) -> dict:
    """组装「拆解落位」命令序列（E1-5）：文件夹「拆解 · <源轨名>」＋音频轨×4＋MIDI 轨×N。

    - stems：{声部: 工程内相对音频路径（audio/ 内）}——调用方先经 Project.import_audio 入库
    - midis：[{stem: 声部, notes: [...], instrument: GM 名（可选）}]；轨名派生「<声部> MIDI」；
      instrument 给定时追加 set_instrument（鼓轨=drums 走 GM 鼓组、贝斯轨=bass）
    - 幂等：该文件夹为系统产物区——重跑先删「folder == 拆解文件夹名」的旧轨（源轨除外），
      再追加重建（重名冲突由命令层自动序号兜底）
    - 索引按「执行到该命令时」的轨表推算（全部追加到末尾；块内序：4 音频 → N MIDI → 挂夹）
    """
    names = [str(_track_attr(t, "name", "") or "") for t in score_tracks]
    folders = [str(_track_attr(t, "folder", "") or "") for t in score_tracks]
    if not (0 <= source_track < len(names)):
        raise ValueError(f"source_track 越界：{source_track}（共 {len(names)} 轨）")
    for s in _SEP_ORDER:
        if not stems.get(s):
            raise ValueError(f"stems 缺声部：{s}")

    source_name = names[source_track] or f"轨道 {source_track + 1}"
    folder_name = f"拆解 · {source_name}"

    olds = [i for i in range(len(names)) if i != source_track and folders[i] == folder_name]

    commands: list[dict] = []
    for i in sorted(olds, reverse=True):
        commands.append({"op": "remove_track", "track": i})

    base = len(names) - len(olds)
    audio_idx: list[int] = []
    for k, stem in enumerate(_SEP_ORDER):
        idx = base + k
        audio_idx.append(idx)
        commands.append({"op": "add_audio_track", "value": {
            "file": str(stems[stem]), "name": _SEP_AUDIO_NAMES[stem]}})
    midi_idx: list[int] = []
    for k, m in enumerate(midis):
        idx = base + len(_SEP_ORDER) + k
        name = _SEP_MIDI_NAMES.get(str(m.get("stem")), "声部 MIDI")
        midi_idx.append(idx)
        commands.append({"op": "add_track", "track": 0, "value": {
            "name": name, "at": idx, "unique": False}})
        inst = m.get("instrument")
        if inst:
            commands.append({"op": "set_instrument", "track": idx,
                             "value": {"program": str(inst)}})
        commands.append({"op": "set_notes", "track": idx,
                         "value": {"notes": m.get("notes") or []}})
    for idx in audio_idx + midi_idx:
        commands.append({"op": "set_track_folder", "track": idx, "value": {"folder": folder_name}})

    return {
        "commands": commands,
        "meta": {
            "folder": folder_name,
            "source_track": source_track,
            "names": [_SEP_AUDIO_NAMES[s] for s in _SEP_ORDER]
                     + [_SEP_MIDI_NAMES.get(str(m.get("stem")), "声部 MIDI") for m in midis],
            "audio_tracks": audio_idx,
            "midi_tracks": midi_idx,
            "removed": sorted(olds),
        },
    }
