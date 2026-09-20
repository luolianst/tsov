"""web_actions.py —— 批B「工具即控件」动作呈现层（ADR-0017）。

服务端统一生成动作卡所需的展示信息（前端不重复实现）：
- 工具名 → 中文标签（TOOL_LABELS）
- 参数摘要（人话版，summarize_args）
- 影响范围（动作前后 score 差异，impact_of；复用 host/diff.diff_notes）

只读/写类判定：
- READ_TOOLS：只读工具（UI 侧折叠为汇总行，不出完整卡）
- SCORE_WRITING_TOOLS：会改 score 的工具（有影响范围，可参与动作级撤销）
"""

from __future__ import annotations

from typing import Any

from .core.score import Score
from .host.diff import diff_notes

TOOL_LABELS: dict[str, str] = {
    "load_score": "读取工程",
    "edit_score": "改谱",
    "set_tempo": "设速度/拍号",
    "render_wav": "渲染音频",
    "play_score": "宿主播放",
    "transcribe": "转录",
    "understand": "参考曲理解",
    "list_dir": "列目录",
    "use_skill": "加载技能",
    "voice_to_score": "转录→工程",
    "detect_key": "调性分析",
    "create_track": "新建轨",
    "write_notes": "写音符",
    "duplicate_bars": "复制小节",
    "set_track_mix": "调音量/声像",
    "apply_effect": "加效果",
    "apply_pattern": "鼓型铺底",
    "analyze_levels": "电平分析",
    "export_audio": "导出音频",
    "export_midi": "导出 MIDI",
    "read_text": "读文件",
}

READ_TOOLS: frozenset[str] = frozenset({
    "load_score", "list_dir", "detect_key", "read_text", "use_skill", "understand",
})

# 会写 score（产生新 JSON 落盘）的工具——动作级撤销的适用范围
SCORE_WRITING_TOOLS: frozenset[str] = frozenset({
    "edit_score", "set_tempo", "voice_to_score", "create_track", "write_notes",
    "duplicate_bars", "set_track_mix", "apply_effect", "apply_pattern",
})


def tool_label(name: str) -> str:
    return TOOL_LABELS.get(name, name)


def _short(text: Any, n: int = 48) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _base(p: Any) -> str:
    s = str(p or "")
    return s.replace("\\", "/").rsplit("/", 1)[-1]


def summarize_args(tool: str, args: dict | None) -> str:
    """参数 → 一句人话摘要（动作卡头部右侧）。缺参数/异常一律降级为安全文本。"""
    a = args if isinstance(args, dict) else {}
    try:
        if tool == "edit_score":
            ann = a.get("annotations")
            if isinstance(ann, list) and ann:
                return f"标注 {len(ann)} 条" + (f" · {_short(a.get('feedback'))}" if a.get("feedback") else "")
            return _short(a.get("feedback")) or "（无反馈文本）"
        if tool == "set_tempo":
            bits = []
            if a.get("tempo") is not None:
                bits.append(f"♩={a['tempo']}")
            if a.get("time_signature"):
                bits.append(str(a["time_signature"]))
            return " · ".join(bits) or "（未给值）"
        if tool == "write_notes":
            notes = a.get("notes")
            n = len(notes) if isinstance(notes, list) else "?"
            ti = a.get("track", 0)
            mode = a.get("mode", "append")
            return f"轨 {ti} · {n} 个音（{mode}）"
        if tool == "duplicate_bars":
            return f"小节 {a.get('src_start_bar')}-{a.get('src_end_bar')} → {a.get('dest_start_bar')}" + (
                f" · 轨 {a['track']}" if a.get("track") is not None else " · 全轨")
        if tool == "set_track_mix":
            bits = [f"轨 {a.get('track', 0)}"]
            if a.get("volume") is not None:
                bits.append(f"音量 {a['volume']}")
            if a.get("pan") is not None:
                bits.append(f"声像 {a['pan']}")
            return " · ".join(bits)
        if tool == "apply_effect":
            return f"轨 {a.get('track', 0)} · {_short(a.get('preset'), 24)}"
        if tool == "apply_pattern":
            bits = [_short(a.get("pattern"), 30)]
            if a.get("start_bar") is not None:
                bits.append(f"自 {a['start_bar']} 小节")
            if a.get("bars") is not None:
                bits.append(f"{a['bars']} 小节")
            if a.get("crash"):
                bits.append("段头镲")
            return " · ".join(bits)
        if tool == "create_track":
            return f"{_short(a.get('name') or a.get('program'), 20)}（{_short(a.get('program'), 20)}）" + (
                f" · 音量 {a['volume']}" if a.get("volume") is not None else "")
        if tool in ("transcribe", "understand"):
            bits = [_base(a.get("audio") or a.get("url"))]
            if a.get("backend"):
                bits.append(str(a["backend"]))
            return " · ".join([b for b in bits if b]) or "（未知输入）"
        if tool == "voice_to_score":
            return _base(a.get("voice_path"))
        if tool in ("render_wav", "export_audio", "export_midi"):
            return _base(a.get("out_wav") or a.get("out"))
        if tool == "analyze_levels":
            return f"窗口 {a['window_sec']}s" if a.get("window_sec") else ""
        if tool in ("load_score", "detect_key"):
            return _base(a.get("score_path"))
        if tool == "read_text":
            return _base(a.get("path"))
        if tool == "list_dir":
            return _base(a.get("path")) or "output/"
        if tool == "use_skill":
            return _short(a.get("name"), 30)
        if tool == "play_score":
            return _base(a.get("score_path"))
    except Exception:  # noqa: BLE001 摘要永不因脏参数炸掉
        return ""
    return ""


def _as_score(x: Any) -> Score | None:
    if x is None:
        return None
    if isinstance(x, Score):
        return x
    if isinstance(x, dict):
        try:
            return Score.from_dict(x)
        except Exception:  # noqa: BLE001
            return None
    return None


def _inst_diff(a: Any, b: Any) -> str:
    """轨道 instrument（program/volume/pan）差异 → 人话；无变化返回空串。"""
    if a is None or b is None:
        return ""
    bits = []
    try:
        if getattr(a, "program", None) != getattr(b, "program", None):
            bits.append(f"音源 {a.program}→{b.program}")
        va, vb = getattr(a, "volume", None), getattr(b, "volume", None)
        if va is not None and vb is not None and abs(float(va) - float(vb)) > 1e-6:
            bits.append(f"音量 {va:.2f}→{vb:.2f}")
        pa, pb = getattr(a, "pan", None), getattr(b, "pan", None)
        if pa is not None and pb is not None and abs(float(pa) - float(pb)) > 1e-6:
            bits.append(f"声像 {pa:+.2f}→{pb:+.2f}")
    except Exception:  # noqa: BLE001
        return ""
    return " · ".join(bits)


def impact_of(pre: Any, post: Any, top: int = 5) -> dict:
    """动作前后 score → 影响范围（"改了哪些轨 / 几个音 / 哪个参数"）。

    pre/post 可为 Score / dict / None；无法解析或内容一致时返回 {"text": "", "tracks": []}。
    """
    a, b = _as_score(pre), _as_score(post)
    out: dict = {"tempo": None, "signature": None, "tracks": [], "text": ""}
    if a is None or b is None:
        return out
    try:
        ta, tb = float(a.tempo or 0), float(b.tempo or 0)
        if abs(ta - tb) > 1e-6:
            out["tempo"] = f"♩{ta:g}→{tb:g}"
        if (a.time_signature or "") != (b.time_signature or ""):
            out["signature"] = f"{a.time_signature or '?'}→{b.time_signature or '?'}"
        n = max(len(a.tracks), len(b.tracks))
        for i in range(n):
            t1 = a.tracks[i] if i < len(a.tracks) else None
            t2 = b.tracks[i] if i < len(b.tracks) else None
            if t1 is None or t2 is None:
                name = (t2 or t1).name if (t2 or t1) else f"轨{i}"
                out["tracks"].append({"index": i, "name": name, "added": len(t2.notes) if t2 else 0,
                                      "removed": len(t1.notes) if t1 else 0, "changed": 0,
                                      "instrument": "轨新增" if t2 else "轨移除"})
                continue
            nd = diff_notes(t1.notes, t2.notes)
            inst = _inst_diff(t1.instrument, t2.instrument)
            if nd.total or inst or t1.name != t2.name:
                out["tracks"].append({"index": i, "name": t2.name, "added": len(nd.added),
                                      "removed": len(nd.removed), "changed": len(nd.changed),
                                      "instrument": inst})
    except Exception:  # noqa: BLE001
        return {"tempo": None, "signature": None, "tracks": [], "text": ""}

    # 人话文本（动作卡"影响"栏；顶部 top 条，其余折叠成"等 N 处"）
    parts: list[str] = []
    if out["tempo"]:
        parts.append(out["tempo"])
    if out["signature"]:
        parts.append(out["signature"])
    for t in out["tracks"][:top]:
        segs = []
        if t["added"]:
            segs.append(f"+{t['added']} 音")
        if t["removed"]:
            segs.append(f"-{t['removed']} 音")
        if t["changed"]:
            segs.append(f"改 {t['changed']} 音")
        if t["instrument"]:
            segs.append(t["instrument"])
        parts.append(f"{t['name']}：" + " ".join(segs))
    extra = len(out["tracks"]) - top
    if extra > 0:
        parts.append(f"等 {extra} 处")
    out["text"] = "；".join(parts)
    return out


# ---------------------------------------------------------------------------
# 动作快照日志（批B B1-2 · 动作级撤销；Q44 弱留存窗口的原型）
# ---------------------------------------------------------------------------

# M-V7 D2（ADR-0019）：ActionJournal 已下沉至 host/journal.py（快照窗口 · 双账本）；
# 此处保留旧导入路径，供既有 import（web.py / 测试）过渡。
from .host.journal import ActionJournal  # noqa: F401
