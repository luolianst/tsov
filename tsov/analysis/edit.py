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

import numpy as np
import requests

from ..core.notes import Note
from ..core.score import KeyCandidate, Score
from ..dsp.pitch import midi_to_hz
from .dataset import midi_to_note_name

from .llm import LLM_ENDPOINT, LLM_MODEL, resolve_api_key  # noqa: E402  与 analysis/llm.py 共用端点/模型（去重）

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


# ---------------------------------------------------------------------------
# 1. 人工标注（最高优先级，程序直接应用）
# ---------------------------------------------------------------------------

def apply_annotations(notes: list[Note], annotations: list[dict]) -> tuple[list[Note], str]:
    """按 annotations 直接改音符。返回 (新音符列表, error)。

    annotation: {index, action, value}
    - pitch:  value=int 改 pitch_midi（pitch_hz=midi_to_hz(value)，deviation_cents 置 0）
    - delete: 删 index 音
    - add:    在 index 处插入新音，value={pitch_midi, start?, end?}（start/end 缺省用前音界）
    - merge:  把 index 与 index+1 合并（时长取并集，音高取前音）
    非法（越界/缺字段/格式错）→ 返回 error，不动原谱。
    """
    if not annotations:
        return notes, ""
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
        else:
            return notes, f"标注 action 未知：{action!r}（可选 pitch/delete/add/merge）"
    return out, ""


# ---------------------------------------------------------------------------
# 2. LLM 编辑
# ---------------------------------------------------------------------------

_EDIT_SYSTEM = (
    "你是 the shape of voice 的音乐编辑助手。给你一份音符序列（JSON 数组）和用户的修改反馈，"
    "以及可选的疑似错音提示。请按反馈修改，并输出**修正后的完整音符 JSON 数组**（不是只输出改动）。"
    "要求：\n"
    "- 每个音符至少含 start（秒，浮点）、end（秒，浮点，start<end）、pitch_midi（整数 0-127）；"
    "可带 deviation_cents / velocity / confidence（未改动的音原样保留原值）。\n"
    "- 不能改的音原样保留（含字段值）。\n"
    "- 若输入的音符序列为空（从零创作）：请按反馈创作新的完整旋律，输出至少 1 个音符。\n"
    "- 只输出 JSON 数组，不要任何解释文字或 Markdown 代码块。"
)


def _build_edit_prompt(notes: list[Note], feedback: str, suspicious: list[dict] | None) -> str:
    rows = []
    for i, n in enumerate(notes):
        rows.append({
            "index": i,
            "start": round(n.start, 3),
            "end": round(n.end, 3),
            "pitch_midi": int(n.pitch_midi),
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


def _call_edit_llm(notes: list[Note], feedback: str, suspicious: list[dict] | None, allow_empty: bool = False, **params) -> tuple[list[Note], str]:
    """调 LLM 编辑，返回 (新音符列表, error)。解析/校验失败 → 拒绝（error 非空，列表为空）。"""
    api_key = resolve_api_key(**params)
    if not api_key:
        return [], "缺少 OPENCODE_GO_API_KEY（LLM 编辑跳过）"
    payload = {
        "model": params.get("model", LLM_MODEL),
        "messages": [
            {"role": "system", "content": _EDIT_SYSTEM},
            {"role": "user", "content": _build_edit_prompt(notes, feedback, suspicious)},
        ],
        "temperature": params.get("temperature", 0.2),
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = float(params.get("timeout", LLM_TIMEOUT_SEC))
    retries = int(params.get("retries", 2))
    last_error = ""
    for _ in range(retries + 1):
        try:
            resp = requests.post(LLM_ENDPOINT, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
            message = body["choices"][0]["message"]
            content = message.get("content") or message.get("reasoning_content") or ""
            if not content.strip():
                last_error = "LLM 空响应"
                continue
            raw = _extract_json_array(content)
            return _validate_llm_notes(raw, allow_empty), ""
        except Exception as e:  # noqa: BLE001 重试/拒绝
            last_error = f"{type(e).__name__}: {e}"
            continue
    return [], f"LLM 编辑失败（重试 {retries} 次后放弃）：{last_error}"


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
        if o.pitch_midi == n.pitch_midi and abs(o.start - n.start) < 1e-6 and abs((o.end - o.start) - (n.end - n.start)) < 1e-6:
            i += 1
            j += 1
            continue
        if o.pitch_midi != n.pitch_midi:
            diffs.append(f"~ idx{j} {_fmt(o)}→{_fmt(n)} ({n.pitch_midi - o.pitch_midi:+d}st)")
        elif abs(o.start - n.start) > 1e-6 or abs((o.end - o.start) - (n.end - n.start)) > 1e-6:
            diffs.append(f"~ idx{j} 时长 {round(o.end - o.start, 2)}→{round(n.end - n.start, 2)}s")
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
        if o.pitch_midi != x.pitch_midi or abs(o.start - x.start) > 1e-6 or abs((o.end - o.start) - (x.end - x.start)) > 1e-6:
            n += 1
    return n


# ---------------------------------------------------------------------------
# 3.5 调性重算（M8：edit 后 key_candidates 同步）
# ---------------------------------------------------------------------------

# 调式音阶（相对根音 pitch class）——scale-fit 调性检测用
_KEY_SCALES = {
    "major": {0, 2, 4, 5, 7, 9, 11},
    "minor": {0, 2, 3, 5, 7, 8, 10},
    "dorian": {0, 2, 3, 5, 7, 9, 10},
}
_NOTE_NAMES12 = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _detect_key(notes: list[Note], top: int = 2) -> list[KeyCandidate]:
    """按新音符 pitch class 直方图重算调性（scale-fit：音阶内音占比 + 主音权重）。

    对短旋律/调式（如 D dorian）比 Krumhansl 相关更稳——D dorian 全音阶内 → 高占比。
    """
    if len(notes) < 4:
        return []
    hist = np.zeros(12)
    for n in notes:
        hist[int(n.pitch_midi) % 12] += 1.0
    total = float(hist.sum()) or 1.0
    h = hist / total

    scored: list[tuple[float, int, str]] = []
    for root in range(12):
        for mode, scale in _KEY_SCALES.items():
            scale_abs = {(s + root) % 12 for s in scale}
            ratio = float(sum(h[pc] for pc in scale_abs))
            tonic_w = float(h[root])
            score = ratio + 0.4 * tonic_w  # 音阶内占比为主，主音出现加分
            scored.append((score, root, f"{_NOTE_NAMES12[root]} {mode}"))

    scored.sort(reverse=True)
    seen_roots: set[int] = set()
    cands: list[KeyCandidate] = []
    for score, root, name in scored:
        if root in seen_roots:
            continue
        seen_roots.add(root)
        conf = round(float(np.clip(score / 1.4, 0.1, 1.0)), 2)  # 归一置信度
        cands.append(KeyCandidate(key=name, confidence=conf))
        if len(cands) >= top:
            break
    return cands


# ---------------------------------------------------------------------------
# 4. 主入口
# ---------------------------------------------------------------------------

def edit_score(
    score: Score,
    feedback: str = "",
    annotations: list[dict] | None = None,
    llm: bool = True,
    suspicious: list[dict] | None = None,
    **params,
) -> EditResult:
    """修正 Score：人工标注 → LLM 编辑 → 校验。

    - annotations：最高优先级，程序直接应用（LLM 不得覆盖标注音——标注已先于 LLM 落定）
    - llm=True 且有 feedback 或 suspicious 时调 LLM 改完整序列
    - 校验失败/LLM 拒绝 → new_score 保留原谱 + error 说明
    """
    score = copy.deepcopy(score)
    if not score.tracks:
        return EditResult(score, [], "Score 无音轨（拒绝编辑）", False)
    track = score.tracks[0]
    orig_notes = list(track.notes)

    # 1. 人工标注
    ann_notes, ann_err = apply_annotations(orig_notes, annotations or [])
    if ann_err:
        return EditResult(score, [], f"人工标注非法（拒绝）：{ann_err}", False)

    # 2. LLM 编辑（在标注结果之上）
    llm_used = False
    error = ""
    result_notes = ann_notes
    if llm and (feedback.strip() or suspicious):
        llm_notes, llm_err = _call_edit_llm(ann_notes, feedback, suspicious,
                                            allow_empty=(len(ann_notes) == 0), **params)
        if llm_err:
            error = llm_err  # 保留标注结果，不阻塞
        else:
            result_notes = llm_notes
            llm_used = True

    # 3. 落定
    track.notes = result_notes
    diff = build_diff_summary(orig_notes, result_notes)
    if result_notes != orig_notes:
        score.key_candidates = _detect_key(result_notes)  # M8：改谱后调性同步
    return EditResult(score, diff, error, llm_used)
