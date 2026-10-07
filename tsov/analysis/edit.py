"""LLM 改谱 + 人工标注编辑（M4 人在环第一轮：音乐对话闭环的"修正执行器"）。

模式定案（ADR-0009）：
- **人工标注**（用户明确指出的音）：程序直接应用，确定性，最高优先级，不经过 LLM
- **LLM 建议 + 用户自然语言反馈**：一起喂 LLM，让它输出**修正后的完整音符序列 JSON**
- **程序校验层**：解析失败/非法输出 → 拒绝并保留原谱（LLM 失败不阻塞）

与 analysis/ 评估模式（LLM 只读，ADR-0004/0005）并存：评估公平性约束 vs 创作辅助。
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.notes import Note
from ..core.score import Instrument, Score, Track
from ..core.units import midi_to_hz
from ..core.names import midi_to_note_name
from ..core.key import detect_key  # 2026-09-22 F1：下沉 core（编辑层/命令层共用）

from ..llm_client import LlmRequestError, chat_post_json, resolve_api_key, resolve_model  # noqa: E402  F4 收口（原经 analysis.llm）

LLM_TIMEOUT_SEC = 120.0  # 编辑调用超时（比分析层 240s 短）

DIFF_CAP = 20  # diff_summary 最多列出的条目数

# 音符表字段（喂 LLM 的紧凑格式；输出只需 start/end/pitch_midi，其余可选）
_NOTE_FIELDS = ("start", "end", "pitch_midi", "deviation_cents", "velocity", "confidence")


@dataclass
class EditResult:
    """编辑结果：修正后的 Score + 人类可读 diff + 错误/降级信息。"""

    new_score: Score
    diff_summary: list[str] = field(default_factory=list)
    error: str = ""  # 非空表示 LLM 编辑被拒绝/降级（new_score 保留原谱）
    llm_used: bool = True
    actions: list[dict] | None = None  # M-V2.2：LLM 返回动作数组时的原始动作（意图级 diff 数据源）
    track_index: int = 0  # 批A P12：作用轨索引（摘要/工具面回显）


# ---------------------------------------------------------------------------
# 1. 人工标注（最高优先级，程序直接应用）
# ---------------------------------------------------------------------------

def apply_annotations(notes: list[Note], annotations: list[dict]) -> tuple[list[Note], str]:
    """按 annotations 直接改音符。返回 (新音符列表, error)。

    annotation: {index, action, value}
    - pitch:    value=int 改 pitch_midi（pitch_hz=midi_to_hz(value)，deviation_cents 置 0）
    - delete:   删 index 音
    - add:      在 index 处插入新音，value={pitch_midi, start?, end?}（start/end 缺省用前音界）
    - merge:    把 index 与 index+1 合并（时长取并集，音高取前音）
    - set_time: value={start?, end?} 改位置/时长（未给字段保持原值）
    - move:      value={start_delta?, end_delta?, pitch_delta?} 相对位移（M-V2.2 动作数组 / 手势同构）
    - velocity:  value=float(0..1) 绝对力度（M-V3：标注队列「力度 ±」）
    非法（越界/缺字段/格式错）→ 返回 error，不动原谱。
    """
    if not annotations:
        # 防别名（审计修 M-V2.3）：返回独立副本，调用方可能就地改返回对象（transpose 路径实测会污染入参）
        return [copy.deepcopy(n) for n in notes], ""
    out = [copy.deepcopy(n) for n in notes]
    for ann in annotations:
        if not isinstance(ann, dict) or "index" not in ann:
            return notes, f"标注缺 index：{ann!r}"
        try:
            idx = int(ann["index"])
        except (TypeError, ValueError):
            return notes, f"标注 index 非法：{ann!r}"
        action = ann.get("action", "pitch")
        if action == "pitch":
            if not (0 <= idx < len(out)):
                return notes, f"pitch 标注越界：index {idx}（共 {len(out)} 音）"
            value = ann.get("value")
            if not isinstance(value, int) or not (0 <= value <= 127):
                return notes, f"pitch 标注 value 非法：{value!r}"
            n = out[idx]
            n.pitch_midi = value
            n.pitch_hz = midi_to_hz(value)
            n.deviation_cents = 0.0
        elif action == "delete":
            if not (0 <= idx < len(out)):
                return notes, f"delete 标注越界：index {idx}"
            out.pop(idx)
        elif action == "add":
            value = ann.get("value")
            if not isinstance(value, dict) or "pitch_midi" not in value:
                return notes, f"add 标注 value 需含 pitch_midi：{value!r}"
            pm = int(value["pitch_midi"])
            if not (0 <= pm <= 127):
                return notes, f"add 标注 pitch_midi 非法：{pm}"
            if not (0 <= idx <= len(out)):
                return notes, f"add 标注越界：index {idx}（允许 0..{len(out)}）"
            start = float(value.get("start", out[idx - 1].end if idx > 0 and out else 0.0))
            end = float(value.get("end", start + 0.3))
            if start >= end:
                return notes, f"add 标注 start>=end：{value!r}"
            out.insert(idx, Note(start=start, end=end, pitch_midi=pm, pitch_hz=midi_to_hz(pm),
                                 deviation_cents=0.0, velocity=0.8, confidence=0.8))
        elif action == "merge":
            if not (0 <= idx < len(out) - 1):
                return notes, f"merge 标注越界：index {idx}（需有下一音）"
            a, b = out[idx], out[idx + 1]
            a.start = min(a.start, b.start)
            a.end = max(a.end, b.end)
            out.pop(idx + 1)
        elif action == "set_time":
            if not (0 <= idx < len(out)):
                return notes, f"set_time 标注越界：index {idx}"
            value = ann.get("value")
            if not isinstance(value, dict):
                return notes, f"set_time 标注 value 需 {{start?, end?}}：{value!r}"
            n = out[idx]
            ns = float(value.get("start", n.start))
            ne = float(value.get("end", n.end))
            if not (ns >= 0 and ns < ne):
                return notes, f"set_time 标注 start/end 非法：{ns}/{ne}"
            n.start = round(ns, 6)
            n.end = round(ne, 6)
        elif action == "move":
            if not (0 <= idx < len(out)):
                return notes, f"move 标注越界：index {idx}"
            value = ann.get("value")
            if not isinstance(value, dict):
                return notes, f"move 标注 value 需 {{start_delta?, end_delta?, pitch_delta?}}：{value!r}"
            n = out[idx]
            sd = float(value.get("start_delta", 0.0))
            ed = float(value.get("end_delta", sd))
            pd = int(value.get("pitch_delta", 0))
            ns = max(0.0, n.start + sd)
            ne = n.end + ed
            pm = max(0, min(127, n.pitch_midi + pd))
            if ns >= ne:
                return notes, f"move 标注后 start>=end：{ns}/{ne}"
            n.start = round(ns, 6)
            n.end = round(ne, 6)
            n.pitch_midi = pm
            n.pitch_hz = midi_to_hz(pm)
            n.deviation_cents = 0.0
        elif action == "velocity":
            if not (0 <= idx < len(out)):
                return notes, f"velocity 标注越界：index {idx}"
            try:
                v = float(ann.get("value"))
            except (TypeError, ValueError):
                return notes, f"velocity 标注 value 非法：{ann.get('value')!r}"
            if not (0.0 <= v <= 1.0):
                return notes, f"velocity 标注越界（应在 0..1）：{v}"
            out[idx].velocity = round(v, 4)
        else:
            return notes, f"标注 action 未知：{action!r}（可选 pitch/delete/add/merge/set_time/move/velocity）"
    return out, ""


# ---------------------------------------------------------------------------
# 2. LLM 编辑
# ---------------------------------------------------------------------------

_EDIT_SYSTEM = (
    "你是 the shape of voice 的音乐编辑助手。给你一份音符序列（JSON 数组）和用户的修改反馈，"
    "以及可选的疑似错音提示。请按反馈修改，并输出**动作 JSON 数组**（意图级编辑，只改需要动的音，"
    "避免重写整谱）。每个动作是一个对象：\n"
    "- 改音高：{\"action\":\"pitch\", \"index\":i, \"value\":整数（0-127，直接替换 pitch_midi）}\n"
    "- 删除音：{\"action\":\"delete\", \"index\":i}\n"
    "- 插入新音：{\"action\":\"add\", \"index\":i, \"value\":{\"pitch_midi\":整数, \"start\":秒, \"end\":秒}}\n"
    "- 改时长/位置：{\"action\":\"set_time\", \"index\":i, \"value\":{\"start\":秒?, \"end\":秒?}}\n"
    "- 改力度：{\"action\":\"velocity\", \"index\":i, \"value\":0..1（线性力度：0 最轻、1 最响）}\n"
    "- 合并相邻音：{\"action\":\"merge\", \"index\":i}（index 与 index+1 合并）\n"
    "- 整体移调：{\"action\":\"transpose\", \"value\":整数（半音，可负）}\n"
    "动作按输入序列的 index 引用（index 从 0 开始）；多个动作按顺序执行（删除/插入会影响后续 index，"
    "请按从后往前的顺序给出 delete 类动作）。要求：\n"
    "- 只输出动作 JSON 数组，不要输出整谱重写，不要任何解释文字或 Markdown 代码块。\n"
    "- 若输入的音符序列为空（从零创作）：输出 add 动作（或默认 output 整谱数组也可以），至少 1 个音符。\n"
    "- 若没有需要改动的音：输出空数组 []。"
    "\n注：set_time 的 value 里未给的字段保持原值；pitch 的 value 是替换后的绝对音高。"
)


def _build_edit_prompt(notes: list[Note], feedback: str, suspicious: list[dict] | None) -> str:
    rows = []
    for i, n in enumerate(notes):
        rows.append({
            "index": i,
            "start": round(n.start, 3),
            "end": round(n.end, 3),
            "pitch_midi": int(n.pitch_midi),
            "velocity": round(n.velocity, 2),
            "deviation_cents": round(n.deviation_cents, 1),
            "confidence": round(n.confidence, 2),
        })
    parts = ["## 音符序列（index 从 0 开始）\n```json\n" + json.dumps(rows, ensure_ascii=False, indent=1) + "\n```"]
    if suspicious:
        parts.append("## 疑似错音提示（供参考，不一定都改）\n```json\n"
                     + json.dumps(suspicious, ensure_ascii=False, indent=1) + "\n```")
    parts.append("## 用户反馈\n" + (feedback or "（无具体反馈，请保持原序列不变）"))
    return "\n\n".join(parts)


def _extract_json_array(text: str) -> list:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                raise ValueError(f"响应不是有效 JSON 数组：{text[:200]!r}") from None
        else:
            raise ValueError(f"响应不是有效 JSON 数组：{text[:200]!r}") from None
    if not isinstance(data, list):
        raise ValueError(f"响应不是 JSON 数组：{type(data).__name__}")
    return data


_ACTION_KINDS = {"pitch", "delete", "add", "merge", "set_time", "move", "velocity", "transpose"}


def _validate_llm_actions(raw: list) -> list[dict]:
    """校验 LLM 输出动作数组；非法 → 抛 ValueError（调用方拒绝并保留原谱）。

    允许空数组（= 无改动）。动作集合与 apply_annotations 语义一致（含命令层同构动词）。
    """
    if not isinstance(raw, list):
        raise ValueError("LLM 输出不是数组（拒绝）")
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i} 个动作不是对象")
        action = item.get("action")
        if action not in _ACTION_KINDS:
            raise ValueError(f"第 {i} 个动作 action 未知：{action!r}（可选 {sorted(_ACTION_KINDS)}）")
        if action == "transpose":
            v = item.get("value")
            try:
                int(v)
            except (TypeError, ValueError):
                raise ValueError(f"transpose value 非法：{v!r}") from None
        else:
            if "index" not in item:
                raise ValueError(f"第 {i} 个动作缺 index：{item!r}")
            try:
                int(item["index"])
            except (TypeError, ValueError):
                raise ValueError(f"第 {i} 个动作 index 非法：{item!r}") from None
        out.append(item)
    return out


def _execute_actions(notes: list[Note], actions: list[dict]) -> tuple[list[Note], str]:
    """执行动作数组（意图级编辑）。transpose 轨级批量；其余走 apply_annotations。

    返回 (新音符列表, error)。任一动作非法 → error 非空，结果未定义（调用方丢弃）。
    """
    if not actions:
        return [copy.deepcopy(n) for n in notes], ""
    anns = [a for a in actions if a.get("action") != "transpose"]
    out, err = apply_annotations(notes, anns)
    if err:
        return notes, err
    # 轨级整体移调（只作用于 track 0 的单轨旋律；按顺序在标注之后）
    for a in actions:
        if a.get("action") != "transpose":
            continue
        st = int(a["value"])
        for n in out:
            if not (0 <= n.pitch_midi + st <= 127):
                return notes, f"transpose 越界：{n.pitch_midi}+{st}"
        for n in out:
            n.pitch_midi += st
            n.pitch_hz = midi_to_hz(n.pitch_midi)
            n.deviation_cents = 0.0
    return out, ""


def build_intent_summary(actions: list[dict], notes: list[Note] | None = None) -> list[str]:
    """意图级人类可读摘要（动作即 diff——不做位置对齐猜测，M-V2.2 议题 ④ 后半）。"""
    if not actions:
        return []
    notes = notes or []
    lines: list[str] = []
    for a in actions:
        act = a.get("action")
        if act == "transpose":
            lines.append(f"~ 整体 {int(a['value']):+d}st")
            continue
        idx = int(a.get("index", -1))
        name = _fmt(notes[idx]) if 0 <= idx < len(notes) else f"idx{idx}"
        if act == "pitch":
            lines.append(f"~ idx{idx} {name}→{_fmt_note_owner(a.get('value'))} ({int(a.get('value', 0)) - (notes[idx].pitch_midi if 0 <= idx < len(notes) else 0):+d}st)")
        elif act == "delete":
            lines.append(f"- idx{idx} {name} (删除)")
        elif act == "add":
            v = a.get("value") or {}
            lines.append(f"+ idx{idx} add {_fmt_note_owner(v.get('pitch_midi')) if isinstance(v, dict) else '?'}:{v.get('start', 0.0) if isinstance(v, dict) else 0.0}s")
        elif act == "merge":
            lines.append(f"~ idx{idx} merge (与 idx{idx + 1})")
        elif act == "set_time":
            v = a.get("value") or {}
            lines.append(f"~ idx{idx} set_time {v}")
        elif act == "move":
            v = a.get("value") or {}
            lines.append(f"~ idx{idx} move {v}")
        elif act == "velocity":
            lines.append(f"~ idx{idx} vel→{a.get('value')}")
    return lines[:DIFF_CAP]


def _fmt_note_owner(value) -> str:
    try:
        return midi_to_note_name(int(value))
    except Exception:  # noqa: BLE001
        return str(value)


def _validate_llm_notes(raw_notes: list, allow_empty: bool = False) -> list[Note]:
    """校验 LLM 输出；非法 → 抛 ValueError（调用方拒绝并保留原谱）。

    allow_empty=True（仅空谱从零创作）：允许空数组（= LLM 未创作，保留原空谱）。
    非空输入仍硬拒空输出（防 LLM 清空已有谱）。
    """
    if not isinstance(raw_notes, list):
        raise ValueError("LLM 输出不是数组（拒绝）")
    if len(raw_notes) == 0:
        if allow_empty:
            return []
        raise ValueError("LLM 输出空数组（拒绝）")
    out: list[Note] = []
    for i, item in enumerate(raw_notes):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i} 个音符不是对象")
        try:
            start = float(item["start"])
            end = float(item["end"])
            pitch = int(item["pitch_midi"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"第 {i} 个音符缺 start/end/pitch_midi 或类型非法") from None
        if not (0 <= pitch <= 127):
            raise ValueError(f"第 {i} 个音符 pitch_midi 越界：{pitch}")
        if not (start >= 0 and start < end):
            raise ValueError(f"第 {i} 个音符 start/end 非法：{start}/{end}")
        out.append(
            Note(
                start=start,
                end=end,
                pitch_midi=pitch,
                pitch_hz=midi_to_hz(pitch),
                velocity=float(item.get("velocity", 0.8)),
                confidence=float(item.get("confidence", 0.8)),
                deviation_cents=float(item.get("deviation_cents", 0.0)),
                is_ornament=bool(item.get("is_ornament", False)),
            )
        )
    return out


def _call_edit_llm(notes: list[Note], feedback: str, suspicious: list[dict] | None, allow_empty: bool = False, **params) -> tuple[list[Note], list[dict] | None, str]:
    """调 LLM 编辑，返回 (新音符列表, 动作数组|None, error)。

    M-V2.2 双模式：
    - 动作数组（含 action 键）→ _validate_llm_actions + _execute_actions（意图级，推荐）
    - 整谱数组（legacy 兼容）→ _validate_llm_notes；actions=None
    - 空数组 → 视为「无改动」（动作语义），返回原谱 + [] + ""
    解析/校验失败 → 拒绝（error 非空，列表为空）。
    """
    api_key = resolve_api_key(**params)
    if not api_key:
        return [], None, "缺少 LLM key（LLM 编辑跳过；可在 WebUI ⚙ 设置 → 对话 / LLM 填写）"
    payload = {
        "model": resolve_model(**params),
        "messages": [
            {"role": "system", "content": _EDIT_SYSTEM},
            {"role": "user", "content": _build_edit_prompt(notes, feedback, suspicious)},
        ],
        "temperature": params.get("temperature", 0.2),
        "response_format": {"type": "json_object"},
    }
    timeout = float(params.get("timeout", LLM_TIMEOUT_SEC))
    retries = int(params.get("retries", 2))
    last_error = ""
    for _ in range(retries + 1):
        try:
            body = chat_post_json(payload, api_key=api_key, timeout=timeout)
            message = body["choices"][0]["message"]
            content = message.get("content") or message.get("reasoning_content") or ""
            if not content.strip():
                last_error = "LLM 空响应"
                continue
            raw = _extract_json_array(content)
            # 空数组 = 动作语义「无改动」（LLM 提示词已要求无改动时返回 []）
            if not raw:
                return [copy.deepcopy(n) for n in notes], [], ""
            # 鉴别：首元素含 action 键 → 动作数组；否则整谱（legacy 兼容）
            if isinstance(raw[0], dict) and "action" in raw[0]:
                actions = _validate_llm_actions(raw)
                new, err = _execute_actions(notes, actions)
                if err:
                    raise ValueError(f"动作执行失败：{err}")
                return new, actions, ""
            return _validate_llm_notes(raw, allow_empty), None, ""
        except LlmRequestError as e:  # noqa: BLE001 网络层失败（已统一包装）→ 重试
            last_error = str(e)
            continue
        except Exception as e:  # noqa: BLE001 重试/拒绝
            last_error = f"{type(e).__name__}: {e}"
            continue
    return [], None, f"LLM 编辑失败（重试 {retries} 次后放弃）：{last_error}"


# ---------------------------------------------------------------------------
# 3. diff_summary（人类可读）
# ---------------------------------------------------------------------------

def _fmt(n: Note) -> str:
    return midi_to_note_name(n.pitch_midi)


def build_diff_summary(orig: list[Note], new: list[Note]) -> list[str]:
    """位置对齐的简单 diff（新增/删除/改音/改时长），最多 DIFF_CAP 条。"""
    diffs: list[str] = []
    i = j = 0
    while (i < len(orig) or j < len(new)) and len(diffs) < DIFF_CAP:
        if j >= len(new):
            diffs.append(f"- idx{i} {_fmt(orig[i])} (删除)")
            i += 1
            continue
        if i >= len(orig):
            diffs.append(f"+ idx{j} {_fmt(new[j])} (新增)")
            j += 1
            continue
        o, n = orig[i], new[j]
        if (o.pitch_midi == n.pitch_midi and abs(o.start - n.start) < 1e-6
                and abs((o.end - o.start) - (n.end - n.start)) < 1e-6
                and abs(o.velocity - n.velocity) < 1e-6):
            i += 1
            j += 1
            continue
        if o.pitch_midi != n.pitch_midi:
            diffs.append(f"~ idx{j} {_fmt(o)}→{_fmt(n)} ({n.pitch_midi - o.pitch_midi:+d}st)")
        elif abs(o.start - n.start) > 1e-6 or abs((o.end - o.start) - (n.end - n.start)) > 1e-6:
            diffs.append(f"~ idx{j} 时长 {round(o.end - o.start, 2)}→{round(n.end - n.start, 2)}s")
        if abs(o.velocity - n.velocity) >= 1e-6:  # 批A P30：力度变更单独出行（原先恒被吞 → 摘要空显「无改动」）
            diffs.append(f"~ idx{j} 力度 {o.velocity:.2f}→{n.velocity:.2f}")
        i += 1
        j += 1
    # 超过 cap 的剩余差异汇总
    total_changes = _count_changes(orig, new)
    if len(diffs) >= DIFF_CAP and total_changes > DIFF_CAP:
        diffs.append(f"…共 {total_changes} 处改动，其余略")
    return diffs


def _count_changes(orig: list[Note], new: list[Note]) -> int:
    n = 0
    for i in range(max(len(orig), len(new))):
        if i >= len(orig) or i >= len(new):
            n += 1
            continue
        o, x = orig[i], new[i]
        if (o.pitch_midi != x.pitch_midi or abs(o.start - x.start) > 1e-6
                or abs((o.end - o.start) - (x.end - x.start)) > 1e-6
                or abs(o.velocity - x.velocity) > 1e-6):  # 批A P30：力度纳入变更计数
            n += 1
    return n


# （调性重算 detect_key 已下沉 tsov/core/key.py —— 编辑层与命令层共用；2026-09-22 F1 自 analysis 迁出）


# ---------------------------------------------------------------------------
# 4. 主入口
# ---------------------------------------------------------------------------


def _resolve_track_index(score: Score, track) -> int:
    """track 选择（索引或轨名）→ 轨索引（缺省 0；批A P12）。

    语义与 tools_compose._resolve_track 一致；未命中直接抛 ValueError（列候选）。
    """
    if track is None:
        return 0
    s = str(track).strip()
    if isinstance(track, int) or s.lstrip("+-").isdigit():
        idx = int(s)
        if 0 <= idx < len(score.tracks):
            return idx
        raise ValueError(f"track 越界：{track!r}（共 {len(score.tracks)} 条）")
    for i, tr in enumerate(score.tracks):
        if tr.name == s:
            return i
    raise ValueError(f"找不到轨道：{track!r}（现有：{[t.name for t in score.tracks]}）")


def edit_score(
    score: Score,
    feedback: str = "",
    annotations: list[dict] | None = None,
    llm: bool = True,
    suspicious: list[dict] | None = None,
    track: int | str | None = None,
    **params,
) -> EditResult:
    """修正 Score：人工标注 → LLM 编辑 → 校验。

    - track：作用轨（索引或轨名；缺省 0）——annotations / LLM 编辑 / 摘要均作用于该轨（批A P12）
    - annotations：最高优先级，程序直接应用（LLM 不得覆盖标注音——标注已先于 LLM 落定）
    - llm=True 且有 feedback 或 suspicious 时调 LLM 改完整序列
    - 校验失败/LLM 拒绝 → new_score 保留原谱 + error 说明
    """
    score = copy.deepcopy(score)
    if not score.tracks:
        # 从零创作（审计修 M-V2.3）：web 新建工程落盘 tracks=[]，原先硬拒会挡住空谱创作 —— 自动补一条空旋律轨
        score.tracks = [Track(name="melody", instrument=Instrument(), notes=[])]
    ti = _resolve_track_index(score, track)
    target = score.tracks[ti]
    orig_notes = list(target.notes)
    snapshot = copy.deepcopy(orig_notes)  # 真值快照（动作路径会就地改对象，不能拿 orig_notes 做前后对比）

    # 1. 人工标注
    ann_notes, ann_err = apply_annotations(orig_notes, annotations or [])
    if ann_err:
        return EditResult(score, [], f"人工标注非法（拒绝）：{ann_err}（作用轨 track[{ti}] {target.name!r}）",
                          False, track_index=ti)

    # 2. LLM 编辑（在标注结果之上）
    llm_used = False
    error = ""
    result_notes = ann_notes
    actions: list[dict] | None = None
    if llm and (feedback.strip() or suspicious):
        llm_notes, llm_actions, llm_err = _call_edit_llm(ann_notes, feedback, suspicious,
                                                         allow_empty=(len(ann_notes) == 0), **params)
        if llm_err:
            error = llm_err  # 保留标注结果，不阻塞
        else:
            result_notes = llm_notes
            actions = llm_actions
            llm_used = True

    # 3. 落定
    # 防误清空守卫（M-V2.4，B2#2 实测漏洞）：非空输入 → 空结果（动作路径 delete-all / 整谱空数组）一律拒绝
    if orig_notes and not result_notes:
        return EditResult(score, [], f"编辑结果为空（拒绝）——非空输入不允许清空，防误删已有谱"
                          f"（作用轨 track[{ti}] {target.name!r}）", llm_used, track_index=ti)

    target.notes = result_notes
    # M-V2.2：动作路径用意图级摘要（动作即 diff），legacy 整谱路径仍位置对齐
    if actions is not None:
        diff = build_intent_summary(actions, ann_notes)
    else:
        diff = build_diff_summary(orig_notes, result_notes)
    if result_notes != snapshot:
        score.key_candidates = detect_key(result_notes)  # M8：改谱后调性同步（与快照比，防别名恒等）；M-V2.4 起与命令层共用 .key 实现
    return EditResult(score, diff, error, llm_used, actions, track_index=ti)
