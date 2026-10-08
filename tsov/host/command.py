"""宿主编辑命令层（ADR-0015）：EditCommand 动词 + EditBatch 事务 + 程序校验。

- 动词第一版：add / remove / set_pitch / set_velocity / set_time / transpose / quantize_time
  （track 级批量 + 单音符精确两种粒度；与 M4「标注精确、LLM 语义、程序校验」同构）
- UI 批A 增补（2026-09-17）：set_tempo（工程级 tempo/拍号） / set_track_mix（volume/pan/mute/solo/bus）
  / set_instrument（program） / add_effect / remove_effect——参数层 UI 与 agent 工具同出，走同一命令通道
- M-V8 E1 增补（2026-09-20）：add_bookmark / remove_bookmark / set_bookmark（书签三层：project/folder/track，
  索引寻址） / set_track_folder（组织层文件夹归属，单层）——段轨转正，UI 手势与外部 agent 同一动作路径
- M-V8 小修包增补（2026-09-21）：scale_time（全谱时间等比缩放＝变速重排，Q47；音符/书签/三层 automation）
  / set_tempo_remap（改 BPM + 按 旧/新 缩放，单命令原子＝「跟速重排」默认入口）
  / remove_track / rename_track——工具与 UI 同一动作路径（工具经 EditBatch）
- M-V8 E2 增补（2026-09-21）：add_audio_track（追加音频轨；file=工程 audio/ 内相对路径）
  / set_audio_track（音频轨改 offset/file——只作用于 kind=="audio"）——音频轨一等公民第一刀
- M-V8 E5 增补（2026-09-23）：split_note（剪刀：at 处切分）/ merge_notes（胶水：与后邻同音高合并，gap≤max_gap）
  / shift_notes（微推：indices 选区或整轨批量时间平移，原子）/ quantize_time 扩展（value 升 {grid, swing?, indices?}，
  swing=后半格顺延比例；裸 grid 数向后兼容）——编辑工具集第一刀
- M-V8 E5 段2：set_track_mix 增 sends（{总线名: 量0~1}，post-fader 支路，整体替换）/ add_effect 与
  remove_effect 增 target（"track" 缺省 | "bus" | "master"，bus 经 ref 指定）——总线效果与 send 返回；
  set_automation（三层点集整体替换：target/ref/param/points；空数组=清除；t 严格递增校验）；
  附：add_bus / remove_bus（Send 前置——总线可建可删，仍被路由/Send 引用时拒删）——混音补齐
- M-V8 E3 段2 增补（2026-09-24）：snap_scale（调内吸附：调外且 |dev|≥阈值的音吸到最近调内音；
  key 可指定/缺省自动检测，threshold_cents 默认 42；indices 选区或整轨）——哼唱快车道 MIDI 处理步同源（core/snap.py）
- M-V8 E3 段2 补（2026-09-25）：add_track（新建空白 MIDI 轨：name 缺省「轨道 N」、重名自动加序号、
  folder 可选）——用户侧「新建轨道」入口与 agent 同一路径（#183② 闭环）
- M-V8 E3 段3 增补（2026-09-25）：add_track 扩展 at（插入位：0..len，缺省追加）/ unique
  （false=允许重名——「转谱进工程」原始轨「与源同名」场景）；set_notes（批量替换轨音符：
  链产物一次落轨；start<end / 音高界内校验、上限 2 万、按 start 排序）——「转谱进工程」
  与手绘/agent 同一动作路径
- M-V8 E4 段2 增补（2026-09-26）：set_effect_params（部分更新既有链中第 k 个效果的参数：
  index 定位 + params 合并 + validate_effect 全量复验）——调参建议「改既有效果」同径落地
- M-V8 E6 段1 增补（2026-09-27 音频编辑刀）：set_audio_clips（音频轨 clip 组整替——切片/修剪/
  移动/淡入淡出/伸缩统一原子落点；src_len=null 表示到文件尾；stretch∈[0.5,2]；fade_in/fade_out 秒）
  / split_audio_clip（时间线 at 秒切分指定 clip：左段保原 id、右段新 id、接缝零间隙）——与 UI 手势同径
- v0.2 批C2 增补（2026-10-08 工作台 v2）：add_lane / remove_lane（Track.lanes 显式道实体：
  param∈注册表 automation 域、重复绑定拒绝；首次显式管理把 automation 现存键**全量物化**；
  移除即失活——曲线数据保留在 dict，非破坏）
- 事务协议：EditBatch.apply(score) 在深拷贝上逐条执行——非法命令被拒绝并记录 error，
  合法命令全部生效（部分应用 + 错误清单）；apply 前不脏原 Score。
- 该命令层 = M-V2 起 agent 工具与 Web UI 共用的编辑通道（docs/05 接口契约）。
"""

from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from ..core.notes import Note
from ..core.score import Bookmark, Bus, Effect, Score, Track
from ..core.snap import snap_out_of_key
from ..core.units import midi_to_hz
from .params import automation_specs


@dataclass
class EditCommand:
    op: str  # add / remove / set_pitch / set_velocity / set_time / transpose / quantize_time / split_note / merge_notes / shift_notes
    track: int = 0
    index: int | None = None  # 单音符命令的目标下标（add 缺省=末尾追加）
    value: Any = None


@dataclass
class BatchResult:
    ok: bool  # 是否全部命令成功
    applied: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "applied": self.applied, "errors": self.errors}


class EditBatch:
    """一组编辑命令（一个事务 = 一轮 LLM 修改 = 一个 git commit）。"""

    def __init__(self, label: str = ""):
        self.label = label
        self.commands: list[EditCommand] = []

    def add(self, op: str, track: int = 0, index: int | None = None, value: Any = None) -> "EditBatch":
        self.commands.append(EditCommand(op=op, track=track, index=index, value=value))
        return self

    def __len__(self) -> int:
        return len(self.commands)

    def apply(self, score: Score) -> tuple[Score, BatchResult]:
        """在深拷贝上执行全部命令 → (新 Score, 结果)。原 Score 不被修改。"""
        out = copy.deepcopy(score)
        errors: list[str] = []
        applied = 0
        for c in self.commands:
            err = _apply_one(out, c)
            if err:
                errors.append(err)
            else:
                applied += 1
        return out, BatchResult(ok=not errors, applied=applied, errors=errors)


def _parse_note_indices(raw: Any, count: int) -> tuple[list[int] | None, str | None]:
    """indices 参数解析（E5 选区批量操作共用）：非空数组、整数、在界内、去重保序。"""
    if not isinstance(raw, (list, tuple)) or len(raw) == 0:
        return None, "indices 需为非空数组（省略=整轨）"
    out: list[int] = []
    for item in raw:
        try:
            j = int(item)
        except (TypeError, ValueError):
            return None, f"indices 含非法下标：{item!r}"
        if not (0 <= j < count):
            return None, f"indices 越界：{j}（共 {count} 音）"
        if j not in out:
            out.append(j)
    return out, None


def _apply_one(score: Score, c: EditCommand) -> str | None:
    """执行单命令 → 错误文本（None=成功）。"""
    # ---- 工程级命令（不需要 track）----
    if c.op == "set_tempo":
        return _apply_set_tempo(score, c)
    # M-V8 小修包 Q47：变速重排（全谱时间缩放；set_tempo_remap = 改 BPM+缩放单命令原子）
    if c.op == "scale_time":
        return _apply_scale_time(score, c)
    if c.op == "set_tempo_remap":
        return _apply_set_tempo_remap(score, c)
    # M-V8 E1：书签三层（工程级索引寻址；与音符共用事务/快照窗口）
    if c.op == "add_bookmark":
        return _apply_add_bookmark(score, c)
    if c.op == "remove_bookmark":
        return _apply_remove_bookmark(score, c)
    if c.op == "set_bookmark":
        return _apply_set_bookmark(score, c)
    # M-V8 E2：音频轨——add 不依赖 track 索引（改轨内字段见 set_audio_track）
    if c.op == "add_audio_track":
        return _apply_add_audio_track(score, c)
    # M-V8 E3 段2：新建空白 MIDI 轨（#183② 用户侧入口闭环）
    if c.op == "add_track":
        return _apply_add_track(score, c)
    # M-V8 E5 段2：自动化（三层寻址：track 用 c.track；bus/master 用 ref/名称；不要求 c.track 在界内）
    if c.op == "set_automation":
        return _apply_set_automation(score, c)
    # M-V8 E5 段2 附：总线管理（Send 前置；master 隐式、不可增删）
    if c.op == "add_bus":
        return _apply_add_bus(score, c)
    if c.op == "remove_bus":
        return _apply_remove_bus(score, c)
    if not (0 <= c.track < len(score.tracks)):
        return f"{c.op} 越界：track {c.track}（共 {len(score.tracks)} 轨）"
    track = score.tracks[c.track]
    notes = track.notes

    # ---- 轨道级参数命令（UI 批A：参数层与 agent 工具同出）----
    if c.op == "remove_track":
        return _apply_remove_track(score, c)
    if c.op == "rename_track":
        return _apply_rename_track(score, track, c)
    # M-V8 E2：音频轨字段（offset/file；只作用于音频轨）
    if c.op == "set_audio_track":
        return _apply_set_audio_track(score, track, c)
    # M-V8 E6 段1：音频编辑刀（clip 组整替 / 语义切分——只作用于音频轨）
    if c.op == "set_audio_clips":
        return _apply_set_audio_clips(score, track, c)
    if c.op == "split_audio_clip":
        return _apply_split_audio_clip(score, track, c)
    if c.op == "set_track_mix":
        return _apply_set_track_mix(score, track, c)
    # M-V8 E3 段3：批量写音符（链产物落轨；轨级批量替换）
    if c.op == "set_notes":
        return _apply_set_notes(score, track, c)
    if c.op == "set_track_folder":
        return _apply_set_track_folder(score, track, c)
    # v0.2 批C2（工作台 v2）：自动化道实体（additive；首次显式管理时物化迁移）
    if c.op == "add_lane":
        return _apply_add_lane(score, track, c)
    if c.op == "remove_lane":
        return _apply_remove_lane(score, track, c)
    if c.op == "set_instrument":
        return _apply_set_instrument(track, c)
    if c.op == "add_effect":
        return _apply_add_effect(score, track, c)
    if c.op == "remove_effect":
        return _apply_remove_effect(score, track, c)
    # M-V8 E4 段2：改既有效果参数（部分合并；调参建议落地）
    if c.op == "set_effect_params":
        return _apply_set_effect_params(score, track, c)

    if c.op == "add":
        v = c.value
        if not isinstance(v, dict) or "pitch_midi" not in v:
            return "add 缺 pitch_midi"
        pm = int(v["pitch_midi"])
        start = float(v.get("start", 0.0))
        end = float(v.get("end", start + 0.3))
        if not (0 <= pm <= 127):
            return f"add pitch_midi 非法：{pm}"
        if start >= end:
            return f"add start>=end：{start}/{end}"
        idx = c.index if c.index is not None else len(notes)
        if not (0 <= idx <= len(notes)):
            return f"add 越界：index {idx}（允许 0..{len(notes)}）"
        notes.insert(
            idx,
            Note(
                start=round(start, 6),
                end=round(end, 6),
                pitch_midi=pm,
                pitch_hz=midi_to_hz(pm),
                velocity=round(float(v.get("velocity", 0.8)), 3),
                confidence=round(float(v.get("confidence", 0.8)), 3),
            ),
        )
        return None

    # 单音符命令需要 index；transpose / quantize_time 是轨级批量，不需要
    if c.op in ("remove", "set_pitch", "set_velocity", "set_time", "split_note", "merge_notes"):
        if c.index is None or not (0 <= c.index < len(notes)):
            return f"{c.op} 越界：index {c.index}（共 {len(notes)} 音）"
    i = c.index

    if c.op == "remove":
        notes.pop(i)
        return None
    if c.op == "set_pitch":
        try:
            pm = int(c.value)
        except (TypeError, ValueError):
            return f"set_pitch 非法：{c.value!r}"
        if not (0 <= pm <= 127):
            return f"set_pitch 越界：{pm}"
        n = notes[i]
        n.pitch_midi = pm
        n.pitch_hz = midi_to_hz(pm)
        n.deviation_cents = 0.0
        return None
    if c.op == "set_velocity":
        try:
            v = float(c.value)
        except (TypeError, ValueError):
            return f"set_velocity 非法：{c.value!r}"
        if not (0.0 <= v <= 1.0):
            return f"set_velocity 越界：{v}"
        notes[i].velocity = round(v, 3)
        return None
    if c.op == "set_time":
        v = c.value
        if not isinstance(v, dict):
            return "set_time value 需 {start?, end?}"
        n = notes[i]
        start = float(v.get("start", n.start))
        end = float(v.get("end", n.end))
        if start >= end:
            return f"set_time start>=end：{start}/{end}"
        n.start = round(start, 6)
        n.end = round(end, 6)
        return None
    # M-V8 E5：剪刀——在 at 处切分（切点两侧各留 ≥1ms；尾段继承全部属性）
    if c.op == "split_note":
        raw = c.value.get("at") if isinstance(c.value, dict) else c.value
        try:
            at = float(raw)
        except (TypeError, ValueError):
            return f"split_note 需要 at（秒）：{raw!r}"
        n = notes[i]
        if not (n.start + 1e-3 <= at <= n.end - 1e-3):
            return f"split_note 切点越界：{at}（音符 {n.start}~{n.end}，须留 ≥1ms）"
        tail = Note(
            start=round(at, 6),
            end=n.end,
            pitch_midi=n.pitch_midi,
            pitch_hz=n.pitch_hz,
            velocity=n.velocity,
            confidence=n.confidence,
            deviation_cents=n.deviation_cents,
            is_ornament=n.is_ornament,
        )
        n.end = round(at, 6)
        notes.insert(i + 1, tail)
        return None
    # M-V8 E5：胶水——与后邻同音高音符合并（gap ≤ max_gap，默认 0.5s；重叠按并集）
    if c.op == "merge_notes":
        max_gap = 0.5
        if isinstance(c.value, dict) and c.value.get("max_gap") is not None:
            try:
                max_gap = float(c.value["max_gap"])
            except (TypeError, ValueError):
                return f"merge_notes max_gap 非法：{c.value['max_gap']!r}"
            if max_gap < 0:
                return f"merge_notes max_gap 越界：{max_gap}"
        n = notes[i]
        best = None
        for j, m in enumerate(notes):
            if j == i or m.pitch_midi != n.pitch_midi:
                continue
            if n.start <= m.start <= n.end + max_gap + 1e-9 and (best is None or m.start < best[1]):
                best = (j, m.start)
        if best is None:
            return f"merge_notes 无相邻同音高音符（gap ≤ {max_gap}s）"
        m = notes[best[0]]
        n.end = round(max(n.end, m.end), 6)
        notes.pop(best[0])
        return None
    if c.op == "transpose":
        try:
            st = int(c.value)
        except (TypeError, ValueError):
            return f"transpose 非法：{c.value!r}"
        for n in notes:
            if not (0 <= n.pitch_midi + st <= 127):
                return f"transpose 越界：{n.pitch_midi}+{st}"
        for n in notes:
            n.pitch_midi += st
            n.pitch_hz = midi_to_hz(n.pitch_midi)
        return None
    if c.op == "quantize_time":
        # M-V8 E5 扩展：value 可为 {grid, swing?, indices?}；裸 grid 数=旧行为（向后兼容）
        v = c.value
        swing = 0.0
        indices = None
        if isinstance(v, dict):
            try:
                grid = int(v.get("grid", 16))
            except (TypeError, ValueError):
                return f"quantize_time 非法 grid：{v.get('grid')!r}"
            if v.get("swing") is not None:
                try:
                    swing = float(v["swing"])
                except (TypeError, ValueError):
                    return f"quantize_time swing 非法：{v['swing']!r}"
                if not (0.0 <= swing <= 1.0):
                    return f"quantize_time swing 越界：{swing}（0~1）"
            if v.get("indices") is not None:
                indices, err = _parse_note_indices(v["indices"], len(notes))
                if err:
                    return err
        else:
            try:
                grid = int(v or 16)
            except (TypeError, ValueError):
                return f"quantize_time 非法：{c.value!r}"
        if grid <= 0:
            return f"quantize_time 非法 grid：{grid}"
        beat_s = 60.0 / (score.tempo or 120.0)
        cell_s = beat_s / grid
        targets = notes if indices is None else [notes[j] for j in indices]
        for n in targets:
            k = round(n.start / cell_s)
            new_start = k * cell_s
            if swing > 0 and k % 2 == 1:  # 奇格位（后半格）→ 顺延 swing×半格
                new_start += swing * (cell_s / 2.0)
            n.start = round(new_start, 6)
            n.end = round(round(n.end / cell_s) * cell_s, 6)
            if n.end <= n.start:
                n.end = round(n.start + cell_s, 6)
        return None
    # M-V8 E3 段2：调内吸附——调外音吸到最近调内音（key 缺省自动检测；indices 选区或整轨）
    if c.op == "snap_scale":
        v = c.value if c.value is not None else {}
        if not isinstance(v, dict):
            return f"snap_scale value 需 {{key?, threshold_cents?, indices?}}：{v!r}"
        raw_key = v.get("key")
        key = str(raw_key) if raw_key not in (None, "", "auto") else None
        try:
            threshold = float(v.get("threshold_cents", 42.0))
        except (TypeError, ValueError):
            return f"snap_scale threshold_cents 非法：{v.get('threshold_cents')!r}"
        if not (0.0 <= threshold <= 100.0):
            return f"snap_scale threshold_cents 越界：{threshold}（0~100）"
        indices = None
        if v.get("indices") is not None:
            indices, err = _parse_note_indices(v["indices"], len(notes))
            if err:
                return err
        targets = notes if indices is None else [notes[j] for j in indices]
        try:
            new_notes, _stats = snap_out_of_key(targets, key=key, threshold_cents=threshold)
        except ValueError as e:
            return f"snap_scale {e}"
        if indices is None:
            notes[:] = new_notes
        else:
            for j, nn in zip(indices, new_notes):
                notes[j] = nn
        return None
    # M-V8 E5：微推——批量时间平移（原子：任一出界整批拒绝；indices 缺省=整轨）
    if c.op == "shift_notes":
        v = c.value
        if not isinstance(v, dict) or v.get("dtime") is None:
            return "shift_notes value 需 {dtime, indices?}"
        try:
            dtime = float(v["dtime"])
        except (TypeError, ValueError):
            return f"shift_notes dtime 非法：{v['dtime']!r}"
        if dtime != dtime or dtime in (float("inf"), float("-inf")):
            return f"shift_notes dtime 非法：{v['dtime']!r}"
        indices = None
        if v.get("indices") is not None:
            indices, err = _parse_note_indices(v["indices"], len(notes))
            if err:
                return err
        targets = notes if indices is None else [notes[j] for j in indices]
        for n in targets:
            if n.start + dtime < -1e-9:
                return f"shift_notes 越界：{n.start} + {dtime} < 0"
        if abs(dtime) < 1e-9:
            return None
        for n in targets:
            n.start = round(n.start + dtime, 6)
            n.end = round(n.end + dtime, 6)
        return None
    return f"未知 op：{c.op}"


# ======================================================================
# UI 批A：工程级 / 轨道级参数命令（与 agent 工具 set_tempo / set_track_mix 同语义）
# ======================================================================


def _apply_set_tempo(score: Score, c: EditCommand) -> str | None:
    """value = {"tempo"?: number, "time_signature"?: "6/8"}（至少一个）。"""
    v = c.value
    if not isinstance(v, dict):
        return "set_tempo value 需 {tempo?, time_signature?}"
    if v.get("tempo") is not None:
        try:
            t = float(v["tempo"])
        except (TypeError, ValueError):
            return f"set_tempo 非法：{v['tempo']!r}"
        if not (20.0 <= t <= 400.0):
            return f"tempo 越界：{t}（20~400）"
        score.tempo = round(t, 3)
    if v.get("time_signature") is not None:
        from ..core.score import parse_time_signature

        raw = str(v["time_signature"]).strip()
        num, den = parse_time_signature(raw)
        if f"{num}/{den}" != raw:
            return f"time_signature 非法（形如 6/8 / 3/4）：{v['time_signature']!r}"
        score.time_signature = f"{num}/{den}"
    if v.get("tempo") is None and v.get("time_signature") is None:
        return "set_tempo 至少需要 tempo 或 time_signature 之一"
    return None


def _apply_set_track_mix(score: Score, track, c: EditCommand) -> str | None:
    """value = {"volume"?: 0~2, "pan"?: -1~1, "mute"?: bool, "solo"?: bool, "bus"?: name}。"""
    v = c.value
    if not isinstance(v, dict):
        return "set_track_mix value 需 {volume?, pan?, mute?, solo?, bus?}"
    known = ("volume", "pan", "mute", "solo", "bus", "sends")
    for k in v:
        if k not in known:
            return f"set_track_mix 未知键：{k!r}（可用：{', '.join(known)}）"
    if v.get("volume") is not None:
        try:
            vol = float(v["volume"])
        except (TypeError, ValueError):
            return f"volume 非法：{v['volume']!r}"
        if not (0.0 <= vol <= 2.0):
            return f"volume 越界：{vol}（0~2）"
        track.instrument.volume = round(vol, 4)
    if v.get("pan") is not None:
        try:
            pan = float(v["pan"])
        except (TypeError, ValueError):
            return f"pan 非法：{v['pan']!r}"
        if not (-1.0 <= pan <= 1.0):
            return f"pan 越界：{pan}（-1~1）"
        track.pan = round(pan, 4)
    if v.get("mute") is not None:
        track.mute = bool(v["mute"])
    if v.get("solo") is not None:
        track.solo = bool(v["solo"])
    if v.get("bus") is not None:
        name = str(v["bus"]).strip() or "master"
        known_buses = {"master"} | {b.name for b in getattr(score, "buses", []) or []}
        if name not in known_buses:
            return f"未知总线：{name!r}（可用：{', '.join(sorted(known_buses))}）"
        track.bus = name
    # M-V8 E5：send 支路 {总线名: 量 0~1}；整体替换（量 0 = 视为移除该支路，不占位）
    if v.get("sends") is not None:
        raw = v["sends"]
        if not isinstance(raw, dict):
            return "set_track_mix sends 需为对象：{总线名: 量}"
        known_buses = {"master"} | {b.name for b in getattr(score, "buses", []) or []}
        sends: dict = {}
        for sname, amt in raw.items():
            s = str(sname).strip()
            if s not in known_buses:
                return f"未知总线：{s!r}（可用：{', '.join(sorted(known_buses))}）"
            try:
                a = float(amt)
            except (TypeError, ValueError):
                return f"sends[{s}] 量非法：{amt!r}"
            if not (0.0 <= a <= 1.0):
                return f"sends[{s}] 量越界：{a}（0~1）"
            if a > 0:
                sends[s] = round(a, 4)
        track.sends = sends
    return None


def _apply_set_instrument(track, c: EditCommand) -> str | None:
    """value = {"program": str}；空串=默认音色；GM 名 / vst3:<路径> / sfz:<路径>。"""
    v = c.value
    if not isinstance(v, dict) or v.get("program") is None:
        return "set_instrument value 需 {program}"
    program = str(v["program"]).strip()
    if program and not program.startswith(("vst3:", "sfz:")):
        from ..midi.export import GM_PROGRAMS

        if program not in GM_PROGRAMS:
            return f"未知音色名 {program!r}（GM 名 / vst3:<路径> / sfz:<路径>，或空串=默认）"
    track.instrument.program = program
    return None


def _effect_target_list(score: Score, track, v: dict):
    """效果目标解析（E5 段2）：target = "track"（缺省）| "bus" | "master"；bus 经 ref 指定名。"""

    target = str(v.get("target") or "track").strip()
    if target == "track":
        return track.instrument.effects, None
    if target == "bus":
        name = str(v.get("ref") or "").strip()
        for b in getattr(score, "buses", []) or []:
            if b.name == name:
                return b.effects, None
        known = sorted({b.name for b in getattr(score, "buses", []) or []} | {"master"})
        return None, f"未知总线：{name!r}（可用：{', '.join(known)}；master 用 target=\"master\"）"
    if target == "master":
        master = getattr(score, "master", None)
        if master is None:
            return None, "master 不存在"
        return master.effects, None
    return None, f"未知 target：{target!r}（track / bus / master）"


def _apply_add_effect(score: Score, track, c: EditCommand) -> str | None:
    """value = {"type": kind, "params"?: {...}, "target"?: "track"|"bus"|"master", "ref"?: 总线名}。"""
    v = c.value
    if not isinstance(v, dict) or not v.get("type"):
        return "add_effect value 需 {type, params?}"
    effects, err = _effect_target_list(score, track, v)
    if err:
        return f"add_effect {err}"
    kind = str(v["type"]).strip()
    from .effect import effect_kinds, validate_effect

    if kind not in effect_kinds():
        return f"未知效果类型：{kind!r}（可用：{', '.join(effect_kinds())}）"
    params = v.get("params") or {}
    if not isinstance(params, dict):
        return "add_effect params 需为对象"
    problems = validate_effect(Effect(type=kind, params=params))
    if problems:
        return "；".join(problems)
    effects.append(Effect(type=kind, params=dict(params)))
    return None


def _apply_remove_effect(score: Score, track, c: EditCommand) -> str | None:
    """index = 效果链下标（用 index 字段；也接受 value 传数字）；target/ref 同 add_effect。"""
    v = c.value if isinstance(c.value, dict) else {}
    effects, err = _effect_target_list(score, track, v)
    if err:
        return f"remove_effect {err}"
    idx = c.index if c.index is not None else (v.get("index") if isinstance(c.value, dict) else c.value)
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return f"remove_effect 需要 index：{idx!r}"
    if not (0 <= idx < len(effects)):
        return f"remove_effect 越界：index {idx}（共 {len(effects)} 个效果）"
    effects.pop(idx)
    return None


def _apply_set_effect_params(score: Score, track, c: EditCommand) -> str | None:
    """value = {"index": k, "params": {...}, "target"?, "ref"?}——部分更新第 k 个效果（E4 段2）。

    合并语义：只覆盖 params 给出的键；合并结果整体过 validate_effect（未知键拒）。
    """
    v = c.value if isinstance(c.value, dict) else {}
    effects, err = _effect_target_list(score, track, v)
    if err:
        return f"set_effect_params {err}"
    idx = c.index if c.index is not None else v.get("index")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return f"set_effect_params 需要 index：{idx!r}"
    if not (0 <= idx < len(effects)):
        return f"set_effect_params 越界：index {idx}（共 {len(effects)} 个效果）"
    params = v.get("params")
    if not isinstance(params, dict) or not params:
        return "set_effect_params params 需为非空对象"
    from .effect import validate_effect

    eff = effects[idx]
    merged = {**dict(eff.params or {}), **params}
    problems = validate_effect(Effect(type=eff.type, params=merged))
    if problems:
        return "；".join(problems)
    eff.params = merged
    return None


# ======================================================================
# M-V8 E1：书签（三层结构数据）+ 文件夹归属（组织层）
# ======================================================================

_BOOKMARK_KINDS = ("section", "mark")


def _bookmark_scope_ok(score: Score, scope: str, ref: str) -> str | None:
    """作用域校验：folder/track 的 ref 必须已存在（folder 由轨道归属隐式定义）。"""
    if scope == "project":
        return None
    if scope == "track":
        if not ref:
            return "track 书签需要 ref（轨道名）"
        names = [t.name for t in score.tracks]
        if ref not in names:
            return f"未知轨道：{ref!r}（可用：{', '.join(names) or '无'}）"
        return None
    if scope == "folder":
        if not ref:
            return "folder 书签需要 ref（文件夹名）"
        folders = sorted({t.folder for t in score.tracks if getattr(t, "folder", "")})
        if ref not in folders:
            return f"未知文件夹：{ref!r}（可用：{', '.join(folders) or '无'}）"
        return None
    return f"未知 scope：{scope!r}（project / folder / track）"


def _apply_add_bookmark(score: Score, c: EditCommand) -> str | None:
    """value = {scope?, ref?, kind?, start, end?, label?, color?}；kind: mark（点）/ section（区间）。"""
    v = c.value
    if not isinstance(v, dict):
        return "add_bookmark value 需 {scope?, ref?, kind?, start, end?, label?}"
    scope = str(v.get("scope") or "project")
    ref = str(v.get("ref") or "")
    err = _bookmark_scope_ok(score, scope, ref)
    if err:
        return err
    kind = str(v.get("kind") or "mark")
    if kind not in _BOOKMARK_KINDS:
        return f"未知 kind：{kind!r}（section / mark）"
    try:
        start = float(v.get("start") or 0.0)
    except (TypeError, ValueError):
        return f"add_bookmark start 非法：{v.get('start')!r}"
    if start < 0:
        return f"add_bookmark start 越界：{start}"
    end = None
    if kind == "section":
        try:
            end = float(v["end"])
        except (KeyError, TypeError, ValueError):
            return "add_bookmark section 需要 end（秒）"
        if end <= start:
            return f"add_bookmark start>=end：{start}/{end}"
    score.bookmarks.append(
        Bookmark(
            scope=scope,
            ref=ref,
            kind=kind,
            start=round(start, 6),
            end=round(end, 6) if end is not None else None,
            label=str(v.get("label") or ""),
            color=str(v.get("color") or ""),
        )
    )
    return None


def _apply_remove_bookmark(score: Score, c: EditCommand) -> str | None:
    """index = score.bookmarks 下标。"""
    idx = c.index if c.index is not None else c.value
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return f"remove_bookmark 需要 index：{idx!r}"
    if not (0 <= idx < len(score.bookmarks)):
        return f"remove_bookmark 越界：index {idx}（共 {len(score.bookmarks)} 个）"
    score.bookmarks.pop(idx)
    return None


def _apply_set_bookmark(score: Score, c: EditCommand) -> str | None:
    """index = 书签下标；value = {scope?, ref?, kind?, start?, end?, label?, color?}（缺省保持原值）。"""
    idx = c.index
    if idx is None or not (0 <= idx < len(score.bookmarks)):
        return f"set_bookmark 越界：index {idx}（共 {len(score.bookmarks)} 个）"
    v = c.value
    if not isinstance(v, dict):
        return "set_bookmark value 需 {scope?, ref?, kind?, start?, end?, label?, color?}"
    bm = score.bookmarks[idx]
    scope = str(v.get("scope") or bm.scope)
    ref = str(v["ref"]) if v.get("ref") is not None else bm.ref
    err = _bookmark_scope_ok(score, scope, ref)
    if err:
        return err
    kind = str(v.get("kind") or bm.kind)
    if kind not in _BOOKMARK_KINDS:
        return f"未知 kind：{kind!r}（section / mark）"
    try:
        start = float(v.get("start", bm.start))
    except (TypeError, ValueError):
        return f"set_bookmark start 非法：{v.get('start')!r}"
    if start < 0:
        return f"set_bookmark start 越界：{start}"
    end = None
    if kind == "section":
        raw_end = v.get("end", bm.end)
        if raw_end is None:
            return "set_bookmark section 需要 end（秒）"
        try:
            end = float(raw_end)
        except (TypeError, ValueError):
            return f"set_bookmark end 非法：{raw_end!r}"
        if end <= start:
            return f"set_bookmark start>=end：{start}/{end}"
    bm.scope, bm.ref, bm.kind = scope, ref, kind
    bm.start = round(start, 6)
    bm.end = round(end, 6) if end is not None else None
    if v.get("label") is not None:
        bm.label = str(v["label"])
    if v.get("color") is not None:
        bm.color = str(v["color"])
    return None


def _apply_set_track_folder(score: Score, track, c: EditCommand) -> str | None:
    """value = {"folder": 名} 或字符串；空串 = 移出文件夹（单层，不嵌套）。"""
    v = c.value
    name = v.get("folder") if isinstance(v, dict) else v
    if name is None:
        return "set_track_folder value 需 {folder} 或字符串"
    name = str(name).strip()
    if len(name) > 64:
        return "文件夹名过长（≤64 字符）"
    track.folder = name
    return None


# ======================================================================
# M-V8 E5 段2：自动化（三层点集整体替换）
# ======================================================================

def _apply_add_bus(score: Score, c: EditCommand) -> str | None:
    """value = {"name"?, "volume"?, "pan"?}——新增混音总线（E5 段2 附：Send 前置）。
    缺省名自动编号 bus1、bus2…；master 为隐式总线（不可新增/占用）。"""

    v = c.value if isinstance(c.value, dict) else {}
    name = str(v.get("name") or "").strip()
    existing = {b.name for b in getattr(score, "buses", []) or []}
    if not name:
        i = 1
        while f"bus{i}" in existing:
            i += 1
        name = f"bus{i}"
    if name == "master":
        return "master 为隐式总线，不能新增同名总线"
    if name in existing:
        return f"总线已存在：{name!r}"
    bus = Bus(name=name)
    for key, lo, hi in (("volume", 0.0, 2.0), ("pan", -1.0, 1.0)):
        val = v.get(key)
        if val is None:
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            return f"add_bus {key} 非法：{val!r}"
        if not (lo <= f <= hi):
            return f"add_bus {key} 越界（{lo}~{hi}）：{f}"
        setattr(bus, key, f)
    score.buses.append(bus)
    return None


def _apply_remove_bus(score: Score, c: EditCommand) -> str | None:
    """value = {"name": str}——移除总线；仍被轨道路由（track.bus）或 Send 引用时拒绝。"""

    v = c.value if isinstance(c.value, dict) else {}
    name = str(v.get("name") or "").strip()
    hit = next((b for b in getattr(score, "buses", []) or [] if b.name == name), None)
    if hit is None:
        return f"总线不存在：{name!r}"
    for t in score.tracks:
        if str(getattr(t, "bus", "master") or "master") == name:
            return f"总线被轨道路由引用（{t.name!r}）——先改 track.bus"
        if name in (getattr(t, "sends", None) or {}):
            return f"总线被 Send 引用（{t.name!r}）——先移除 Send"
    score.buses.remove(hit)
    return None


# v0.2 批C 前段（R1 地基件）：自动化参数域由 host/params.py 单源派生——
# 键集/钳制/顺序（volume → pan）与历史一字不差；错误文案同源（_AUTOMATION_NAMES）。
_AUTOMATION_PARAMS = {pid: (spec.lo, spec.hi) for pid, spec in automation_specs().items()}
_AUTOMATION_NAMES = " / ".join(_AUTOMATION_PARAMS)


def _apply_set_automation(score: Score, c: EditCommand) -> str | None:
    """value = {"target"?: "track"|"bus"|"master", "ref"?: 总线名, "param": "volume"|"pan",
    "points": [[t, v], …]}——目标参数的整个点集替换（空数组 = 清除；t≥0 严格递增；v 按参数域校验）。"""

    v = c.value
    if not isinstance(v, dict):
        return "set_automation value 需 {target?, ref?, param, points}"
    target = str(v.get("target") or "track").strip()
    if target == "track":
        if not (0 <= c.track < len(score.tracks)):
            return f"set_automation 越界：track {c.track}（共 {len(score.tracks)} 轨）"
        host = score.tracks[c.track]
    elif target == "bus":
        name = str(v.get("ref") or "").strip()
        host = next((b for b in getattr(score, "buses", []) or [] if b.name == name), None)
        if host is None:
            known = sorted({b.name for b in getattr(score, "buses", []) or []} | {"master"})
            return f"未知总线：{name!r}（可用：{', '.join(known)}；master 用 target=\"master\"）"
    elif target == "master":
        host = getattr(score, "master", None)
        if host is None:
            return "master 不存在"
    else:
        return f"未知 target：{target!r}（track / bus / master）"
    param = str(v.get("param") or "").strip()
    if param not in _AUTOMATION_PARAMS:
        return f"未知 param：{param!r}（{_AUTOMATION_NAMES}）"
    raw = v.get("points")
    if raw is None:
        return "set_automation 需要 points（数组；空数组=清除）"
    if not isinstance(raw, (list, tuple)):
        return "set_automation points 需为数组 [[t, v], …]"
    lo, hi = _AUTOMATION_PARAMS[param]
    pts: list[list[float]] = []
    prev = None
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            return f"set_automation 点非法（需 [t, v]）：{item!r}"
        try:
            t = float(item[0])
            val = float(item[1])
        except (TypeError, ValueError):
            return f"set_automation 点非法：{item!r}"
        if t < 0:
            return f"set_automation 时间点越界：{t}"
        if prev is not None and t <= prev:
            return f"set_automation 时间点须严格递增：{prev} → {t}"
        if not (lo <= val <= hi):
            return f"set_automation {param} 值越界：{val}（{lo}~{hi}）"
        pts.append([round(t, 6), round(val, 4)])
        prev = t
    automation = dict(getattr(host, "automation", None) or {})
    if pts:
        automation[param] = pts
    else:
        automation.pop(param, None)
    host.automation = automation
    return None


# ======================================================================
# M-V8 小修包（2026-09-21）：变速重排（Q47） + 轨道管理（G5 遗留）
# ======================================================================


def _scale_automation(automation: dict, factor: float) -> int:
    """automation {key: [[t, v], ...]} → t 等比缩放；返回缩放点数。"""
    count = 0
    for pts in (automation or {}).values():
        for p in pts:
            p[0] = round(float(p[0]) * factor, 6)
            count += 1
    return count


def scale_score_times(score: Score, factor: float) -> dict:
    """全谱时间等比缩放（Q47 变速重排）：音符 + 书签 + 三层 automation（track/bus/master）。

    新时间 = 旧时间 × factor（factor = 旧tempo ÷ 新tempo；>1 变慢、<1 变快）。
    等比缩放下音符与网格的相对关系不变（旧对齐 = 新对齐，仅整体时间轴缩放）。
    返回统计 {"notes", "bookmarks", "automation_points"}。
    """
    stats = {"notes": 0, "bookmarks": 0, "automation_points": 0}
    for track in score.tracks:
        for n in track.notes:
            n.start = round(n.start * factor, 6)
            n.end = round(n.end * factor, 6)
            stats["notes"] += 1
        stats["automation_points"] += _scale_automation(track.automation, factor)
    for bus in getattr(score, "buses", []) or []:
        stats["automation_points"] += _scale_automation(bus.automation, factor)
    master = getattr(score, "master", None)
    if master is not None:
        stats["automation_points"] += _scale_automation(master.automation, factor)
    for bm in score.bookmarks:
        bm.start = round(bm.start * factor, 6)
        if bm.end is not None:
            bm.end = round(bm.end * factor, 6)
        stats["bookmarks"] += 1
    return stats


def _apply_scale_time(score: Score, c: EditCommand) -> str | None:
    """value = {"factor": >0}（也接受裸数字）；全谱时间 × factor。"""
    v = c.value
    raw = v.get("factor") if isinstance(v, dict) else v
    if raw is None:
        return "scale_time value 需 {factor}（>0；新时长=旧×factor）"
    try:
        factor = float(raw)
    except (TypeError, ValueError):
        return f"scale_time factor 非法：{raw!r}"
    if not (0.01 <= factor <= 100.0):
        return f"scale_time factor 越界：{factor}（0.01~100）"
    scale_score_times(score, factor)
    return None


def _apply_set_tempo_remap(score: Score, c: EditCommand) -> str | None:
    """value = {"tempo": x, "time_signature"?: "n/d"}；改 BPM 并按 旧/新 全谱等比缩放（Q47 变速重排）。

    单命令原子：任一部分校验不通过 → 整体拒绝，不会出现「缩放了但 tempo 没改」的分裂状态。
    """
    v = c.value
    if not isinstance(v, dict) or v.get("tempo") is None:
        return "set_tempo_remap value 需 {tempo, time_signature?}"
    # 1) 全部校验（不动 score）
    try:
        t = float(v["tempo"])
    except (TypeError, ValueError):
        return f"set_tempo_remap tempo 非法：{v['tempo']!r}"
    if not (20.0 <= t <= 400.0):
        return f"set_tempo_remap tempo 越界：{t}（20~400）"
    ts_norm = None
    if v.get("time_signature") is not None:
        from ..core.score import parse_time_signature

        raw = str(v["time_signature"]).strip()
        num, den = parse_time_signature(raw)
        if f"{num}/{den}" != raw:
            return f"set_tempo_remap time_signature 非法（形如 6/8 / 3/4）：{v['time_signature']!r}"
        ts_norm = f"{num}/{den}"
    # 2) 校验通过 → 一次性执行（tempo + 拍号 + 缩放）
    old = float(score.tempo or 120.0)
    score.tempo = round(t, 3)
    if ts_norm is not None:
        score.time_signature = ts_norm
    if abs(t - old) > 1e-9:
        scale_score_times(score, old / t)
    return None


def _apply_remove_track(score: Score, c: EditCommand) -> str | None:
    """track = 索引；删轨并同步清理引用（该轨 track 层书签；文件夹变空时连 folder 书签）。"""
    track = score.tracks[c.track]
    name = track.name
    folder = getattr(track, "folder", "") or ""
    score.tracks.pop(c.track)
    if score.bookmarks:
        folders_after = {t.folder for t in score.tracks if getattr(t, "folder", "")}
        score.bookmarks = [
            bm for bm in score.bookmarks
            if not (bm.scope == "track" and bm.ref == name)
            and not (bm.scope == "folder" and folder and bm.ref == folder and folder not in folders_after)
        ]
    return None


def _apply_rename_track(score: Score, track, c: EditCommand) -> str | None:
    """value = {"name": str} 或字符串；重名拒绝；同步更新该轨 track 层书签 ref。"""
    v = c.value
    name = v.get("name") if isinstance(v, dict) else v
    if name is None:
        return "rename_track value 需 {name} 或字符串"
    name = str(name).strip()
    if not name:
        return "rename_track 名称不能为空"
    if len(name) > 64:
        return "rename_track 名称过长（≤64 字符）"
    if name == track.name:
        return None
    if any(t is not track and t.name == name for t in score.tracks):
        return f"rename_track 名称已存在：{name!r}"
    old = track.name
    track.name = name
    for bm in score.bookmarks:
        if bm.scope == "track" and bm.ref == old:
            bm.ref = name
    return None


# ---------------------------------------------------------------------------
# M-V8 E2：音频轨（audio track · 第一刀）
# ---------------------------------------------------------------------------


def _audio_track_path_guard(rel: str) -> str | None:
    """音频相对路径护栏：非空 / 非绝对 / 无穿越 / 须在 audio/ 下。返回错误文本或 None。"""
    if not rel:
        return "音频路径为空"
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or (p.parts and p.parts[0] != "audio"):
        return f"须为工程 audio/ 内相对路径：{rel!r}"
    return None


def _audio_default_name(rel: str) -> str:
    """从 file 生成缺省轨名：``audio/原曲-1a2b3c4d.flac`` → ``原曲``（去 8 位哈希后缀）。"""
    stem = PurePosixPath(rel).stem
    parts = stem.rsplit("-", 1)
    if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{8}", parts[1]):
        stem = parts[0]
    return (stem or "音频")[:64]


def _lane_next_id(lanes: list) -> str:
    used = {str(l.get("id") or "") for l in lanes if isinstance(l, dict)}
    k = 1
    while f"l{k}" in used:
        k += 1
    return f"l{k}"


def _materialize_lanes(track: Track) -> None:
    """首次显式管理（加/删道）时把 automation dict 现存键**全量**物化为 lanes（volume/pan 在前）。

    此后以 lanes 为准（求值范围 = lanes；曲线数据保留在 dict 中——「移除即失活」天然非破坏）。"""
    if getattr(track, "lanes", None) is not None:
        return
    auto = track.automation or {}
    keys = sorted(auto.keys(), key=lambda k: (0 if k == "volume" else 1 if k == "pan" else 2, str(k)))
    track.lanes = [{"id": f"l{i}", "param": str(k)} for i, k in enumerate(keys, start=1)]


def _apply_add_lane(score: Score, track: Track, c: EditCommand) -> str | None:
    """value = {param, name?}；param 须 ∈ 注册表 automation 域（automatable）；重复绑定拒绝。

    首次显式管理 → 先物化现存键（见 _materialize_lanes）；被拒时零副作用（不提前物化）。"""
    v = c.value if isinstance(c.value, dict) else {}
    param = str(v.get("param") or "").strip()
    allowed = [s.id for s in automation_specs().values() if s.automatable]
    if param not in allowed:
        return f"add_lane 未知/不可自动化参数：{param!r}（可用：{allowed}）"
    lanes = track.lanes
    if lanes is None:
        if param in {str(k) for k in (track.automation or {})}:
            return f"add_lane 重复绑定：{param!r} 已在道中"
        _materialize_lanes(track)
        lanes = track.lanes or []
    if any(str(l.get("param")) == param for l in lanes):
        return f"add_lane 重复绑定：{param!r} 已在道中"
    entry: dict = {"id": _lane_next_id(lanes), "param": param}
    name = str(v.get("name") or "").strip()
    if name:
        entry["name"] = name[:64]
    lanes.append(entry)
    track.lanes = lanes
    return None


def _apply_remove_lane(score: Score, track: Track, c: EditCommand) -> str | None:
    """value = {id} 或 {param}；从 lanes 移除即**失活**（曲线数据保留在 automation dict，非破坏）。

    未找到时零副作用（不提前物化）。"""
    v = c.value if isinstance(c.value, dict) else {}
    key = str(v.get("id") or v.get("param") or "").strip()
    if not key:
        return "remove_lane 需 {id} 或 {param}"
    lanes = track.lanes
    if lanes is None:
        # 物化形态预览（不落盘）：未管理时 id 与 param 同词（volume/pan/…），命中才物化
        if not any(str(k) == key for k in (track.automation or {})):
            return f"remove_lane 未找到道：{key!r}"
        _materialize_lanes(track)
        lanes = track.lanes or []
    for i, l in enumerate(lanes):
        if isinstance(l, dict) and (str(l.get("id")) == key or str(l.get("param")) == key):
            del lanes[i]
            track.lanes = lanes
            return None
    return f"remove_lane 未找到道：{key!r}"


def _apply_add_track(score: Score, c: EditCommand) -> str | None:
    """value = {name?, folder?, at?, unique?}；插入一条空白 MIDI 轨（手绘/编辑/转谱进工程共用）。

    - name 缺省 = 「轨道 N」（N = 现轨数 + 1）
    - unique 缺省 true：与现有轨重名 → 自动加序号（2, 3, …）；
      unique=false：允许重名（「转谱进工程」原始轨「与源同名」场景）
    - at：插入位置（0..len；缺省=末尾追加）——「排在源轨正下方」用
    - folder 可选：组织层文件夹归属（单层；空 = 无归属）
    """
    v = c.value if isinstance(c.value, dict) else {}
    name = str(v.get("name") or "").strip() or f"轨道 {len(score.tracks) + 1}"
    if len(name) > 64:
        return "add_track 名称过长（≤64 字符）"
    if bool(v.get("unique", True)):
        existing = {t.name for t in score.tracks}
        base, i = name, 2
        while name in existing:
            name = f"{base} {i}"
            i += 1
    at_raw = v.get("at")
    if at_raw is None:
        at = len(score.tracks)
    else:
        try:
            at = int(at_raw)
        except (TypeError, ValueError):
            return f"add_track at 非法：{at_raw!r}"
        if not (0 <= at <= len(score.tracks)):
            return f"add_track at 越界：{at}（允许 0..{len(score.tracks)}）"
    tr = Track(name=name)
    folder = str(v.get("folder") or "").strip()
    if folder:
        tr.folder = folder[:64]
    score.tracks.insert(at, tr)
    return None


def _apply_set_notes(score: Score, track, c: EditCommand) -> str | None:
    """value = {notes: [...]}（或裸数组）；批量替换该轨全部音符（链产物落轨）。

    - 音符字段：{start, end, pitch_midi, pitch_hz?, velocity?, confidence?, deviation_cents?, is_ornament?}
    - 校验：start<end / 0≤pitch_midi≤127 / 数量 ≤ 20000（护栏）；pitch_hz 缺省按等分音律补齐
    - 只作用于 MIDI 轨；落轨后按 start 排序（链产物通常已序）
    """
    if str(getattr(track, "kind", "midi") or "midi") != "midi":
        return f"set_notes 只能写 MIDI 轨（track {c.track} kind={getattr(track, 'kind', 'midi')!r}）"
    v = c.value
    notes_raw = v.get("notes") if isinstance(v, dict) else v
    if not isinstance(notes_raw, list):
        return "set_notes value 需 {notes: [...]} 或音符数组"
    if len(notes_raw) > 20000:
        return f"set_notes 数量超限：{len(notes_raw)}（≤20000）"
    out: list[Note] = []
    for k, item in enumerate(notes_raw):
        if not isinstance(item, dict) or "pitch_midi" not in item:
            return f"set_notes 第 {k} 条缺 pitch_midi"
        try:
            pm = int(item["pitch_midi"])
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start + 0.3))
            vel = round(float(item.get("velocity", 0.8)), 3)
            conf = round(float(item.get("confidence", 0.8)), 3)
        except (TypeError, ValueError):
            return f"set_notes 第 {k} 条字段非法：{item!r}"
        if not (0 <= pm <= 127):
            return f"set_notes 第 {k} 条 pitch_midi 非法：{pm}"
        if start >= end:
            return f"set_notes 第 {k} 条 start>=end：{start}/{end}"
        try:
            hz = float(item.get("pitch_hz") or 0.0) or midi_to_hz(pm)
        except (TypeError, ValueError):
            hz = midi_to_hz(pm)
        note = Note(start=round(start, 6), end=round(end, 6), pitch_midi=pm,
                    pitch_hz=round(hz, 6), velocity=vel, confidence=conf)
        dev = item.get("deviation_cents")
        if dev is not None:
            try:
                note.deviation_cents = round(float(dev), 3)
            except (TypeError, ValueError):
                pass
        if bool(item.get("is_ornament", False)):
            note.is_ornament = True
        out.append(note)
    out.sort(key=lambda n: n.start)
    track.notes = out
    return None


def _apply_add_audio_track(score: Score, c: EditCommand) -> str | None:
    """value = {file, offset?, name?}；追加一条音频轨（kind="audio"）。

    - file：工程内相对路径（须在 audio/ 下、不得穿越）——**文件存在性由宿主层运行时校验**
    - name 缺省 = file 去哈希后缀；与现有轨重名 → 自动加序号（2, 3, …）
    """
    v = c.value
    if not isinstance(v, dict):
        return "add_audio_track value 需 {file, offset?, name?}"
    rel = str(v.get("file") or "").strip().replace("\\", "/")
    err = _audio_track_path_guard(rel)
    if err:
        return f"add_audio_track {err}"
    try:
        offset = float(v.get("offset") or 0.0)
    except (TypeError, ValueError):
        return f"add_audio_track offset 非法：{v.get('offset')!r}"
    if offset < 0:
        return f"add_audio_track offset 不能为负：{offset}"
    name = str(v.get("name") or "").strip() or _audio_default_name(rel)
    if len(name) > 64:
        return "add_audio_track 名称过长（≤64 字符）"
    existing = {t.name for t in score.tracks}
    base, i = name, 2
    while name in existing:
        name = f"{base} {i}"
        i += 1
    score.tracks.append(Track(name=name, kind="audio", audio={"file": rel, "offset": round(offset, 6)}))
    return None


def _apply_set_audio_track(score: Score, track, c: EditCommand) -> str | None:
    """track = 索引；value = {offset?, file?}——只作用于音频轨（kind=="audio"）。

    E6：轨道已是 clip 形态时——offset/file 映射到唯一 clip（多 clip → 拒绝，请用 set_audio_clips）。
    """
    if str(getattr(track, "kind", "midi") or "midi") != "audio":
        return f"set_audio_track 只作用于音频轨（track {c.track} 是 MIDI 轨）"
    v = c.value
    if not isinstance(v, dict):
        return "set_audio_track value 需 {offset?, file?}"
    audio = dict(getattr(track, "audio", None) or {})
    if "clips" in audio:
        clips = track.audio_clips()
        if len(clips) != 1:
            return f"set_audio_track 该轨道为多 clip（{len(clips)} 条），请用 set_audio_clips"
        cl = dict(clips[0])
        changed = False
        if "offset" in v:
            try:
                off = float(v["offset"])
            except (TypeError, ValueError):
                return f"set_audio_track offset 非法：{v['offset']!r}"
            if off < 0:
                return f"set_audio_track offset 不能为负：{off}"
            cl["start"] = round(off, 6)
            changed = True
        if "file" in v:
            rel = str(v.get("file") or "").strip().replace("\\", "/")
            err = _audio_track_path_guard(rel)
            if err:
                return f"set_audio_track {err}"
            cl["file"] = rel
            changed = True
        if not changed:
            return "set_audio_track 无可改字段（offset / file）"
        track.audio = {"clips": [cl]}
        return None
    changed = False
    if "offset" in v:
        try:
            off = float(v["offset"])
        except (TypeError, ValueError):
            return f"set_audio_track offset 非法：{v['offset']!r}"
        if off < 0:
            return f"set_audio_track offset 不能为负：{off}"
        audio["offset"] = round(off, 6)
        changed = True
    if "file" in v:
        rel = str(v.get("file") or "").strip().replace("\\", "/")
        err = _audio_track_path_guard(rel)
        if err:
            return f"set_audio_track {err}"
        audio["file"] = rel
        changed = True
    if not changed:
        return "set_audio_track 无可改字段（offset / file）"
    track.audio = audio
    return None


# ---------------------------------------------------------------------------
# M-V8 E6 段1：音频编辑刀（clip 组整替 / 语义切分）
# ---------------------------------------------------------------------------


def _validate_clip(v: dict, idx: int) -> tuple[dict | None, str | None]:
    """单条 clip 字段校验 → (规范化 clip, 错误文本)。"""
    rel = str(v.get("file") or "").strip().replace("\\", "/")
    err = _audio_track_path_guard(rel)
    if err:
        return None, f"clips[{idx}] {err}"

    def _num(key: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
        raw = v.get(key)
        if raw is None:
            val = default
        else:
            try:
                val = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"clips[{idx}] {key} 非法：{raw!r}") from None
        if lo is not None and val < lo:
            raise ValueError(f"clips[{idx}] {key} 越界（≥{lo}）：{val}")
        if hi is not None and val > hi:
            raise ValueError(f"clips[{idx}] {key} 越界（≤{hi}）：{val}")
        return val

    try:
        start = _num("start", 0.0, lo=0.0)
        src_offset = _num("src_offset", 0.0, lo=0.0)
        stretch = _num("stretch", 1.0, lo=0.5, hi=2.0)
        fade_in = _num("fade_in", 0.0, lo=0.0)
        fade_out = _num("fade_out", 0.0, lo=0.0)
        src_len = None
        if v.get("src_len") is not None:
            src_len = _num("src_len", 0.0, lo=1e-4)
    except ValueError as exc:
        return None, str(exc)
    clip_id = str(v.get("clip_id") or "").strip()
    if len(clip_id) > 64:
        return None, f"clips[{idx}] clip_id 过长（≤64 字符）"
    return (
        {
            "clip_id": clip_id,
            "file": rel,
            "start": round(start, 6),
            "src_offset": round(src_offset, 6),
            "src_len": None if src_len is None else round(src_len, 6),
            "stretch": round(stretch, 6),
            "fade_in": round(fade_in, 6),
            "fade_out": round(fade_out, 6),
        },
        None,
    )


def _apply_set_audio_clips(score: Score, track, c: EditCommand) -> str | None:
    """track=索引；value={clips:[…]}——整组替换音频轨 clip 列表（E6：切片/修剪/移动/淡化/伸缩统一原子落点）。

    clip = {clip_id?, file, start≥0, src_offset≥0, src_len(null=到文件尾,>0),
    stretch∈[0.5,2], fade_in≥0, fade_out≥0}；空数组 = 清空（静音轨）。
    """
    if str(getattr(track, "kind", "midi") or "midi") != "audio":
        return f"set_audio_clips 只作用于音频轨（track {c.track} 是 MIDI 轨）"
    v = c.value
    if not isinstance(v, dict) or not isinstance(v.get("clips"), list):
        return "set_audio_clips value 需 {clips: [...]}"
    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(v["clips"]):
        if not isinstance(item, dict):
            return f"set_audio_clips clips[{i}] 需为对象"
        clip, err = _validate_clip(item, i)
        if err:
            return f"set_audio_clips {err}"
        if not clip["clip_id"]:
            clip["clip_id"] = uuid.uuid4().hex[:8]
        if clip["clip_id"] in seen:
            return f"set_audio_clips clip_id 重复：{clip['clip_id']!r}"
        seen.add(clip["clip_id"])
        out.append(clip)
    track.audio = {"clips": out}
    return None


def _apply_split_audio_clip(score: Score, track, c: EditCommand) -> str | None:
    """track=索引；value={clip_id, at}——时间线 at 秒处切分 clip（E6；接缝零间隙）。

    左段保留原 clip_id 与 fade_in、源区间 [src_offset, +Δ/k]；右段新 id、继承 fade_out、
    源区间顺移（已知 src_len 时按剩余收缩；null 保持「到文件尾」）。at 须 ∈ (start+1ms, end−1ms)（src_len=null 时仅校验下界）。
    """
    if str(getattr(track, "kind", "midi") or "midi") != "audio":
        return f"split_audio_clip 只作用于音频轨（track {c.track} 是 MIDI 轨）"
    v = c.value
    if not isinstance(v, dict):
        return "split_audio_clip value 需 {clip_id, at}"
    clip_id = str(v.get("clip_id") or "").strip()
    try:
        at = float(v.get("at"))
    except (TypeError, ValueError):
        return f"split_audio_clip at 非法：{v.get('at')!r}"
    clips = track.audio_clips()
    if clip_id:
        idx = next((i for i, cl in enumerate(clips) if cl["clip_id"] == clip_id), None)
    elif len(clips) == 1:  # UI 便捷：空 id = 唯一 clip（旧形态未迁移/单片段轨道）
        idx = 0
    else:
        return "split_audio_clip 多 clip 轨道须指定 clip_id"
    if idx is None:
        return f"split_audio_clip 未找到 clip：{clip_id!r}"
    cl = clips[idx]
    k = float(cl["stretch"]) or 1.0
    start = float(cl["start"])
    if at <= start + 0.001:
        return f"split_audio_clip 切点须在 clip 起点之后：at={at} start={start}"
    if cl["src_len"] is not None:
        end = start + float(cl["src_len"]) * k
        if at >= end - 0.001:
            return f"split_audio_clip 切点须在 clip 终点之前：at={at} end={end}"
    left_src = (at - start) / k
    left = dict(cl)
    left["src_len"] = round(left_src, 6)
    left["fade_out"] = 0.0
    right = dict(cl)
    right["clip_id"] = uuid.uuid4().hex[:8]
    right["start"] = round(at, 6)
    right["src_offset"] = round(float(cl["src_offset"]) + left_src, 6)
    if cl["src_len"] is not None:  # 已知长度：右段按剩余源区间收缩（null = 到文件尾，保持）
        right["src_len"] = round(float(cl["src_len"]) - left_src, 6)
    right["fade_in"] = 0.0
    clips[idx : idx + 1] = [left, right]
    track.audio = {"clips": clips}
    return None
