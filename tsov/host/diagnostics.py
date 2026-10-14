"""插件静音体检（P62a，2026-10-14）：渲染后检测「有音符但全零」的 VST3 轨。

- 判定三条件：`vst3:` 音源 ∧ 轨含 ≥1 音符 ∧ 渲染峰值 ≤ 噪声地板（默认 -120 dBFS）。
- 数据来源：`render_buses(stats=...)` 采集的 `stats["stem_peaks"]`——复用现成 stem 缓冲区，
  零额外渲染；mute / solo 门控 / 文件夹静音等「合法静音」轨根本不会被采集（天然排除）。
- 输出：结构化 warnings（`code=instrument_silent`），供 web 渲染响应 / MCP 工具文本 /
  后续 P62b 过桥通道共用。
- 指引文案（hint）：优先读 `presets/v3-maps/<slug>.json` 的 `meta.notes`（策展层，Q5 默认），
  缺省回退通用文案。
"""

from __future__ import annotations

import math
from pathlib import Path

from . import plugin_maps

SILENCE_FLOOR_DBFS = -120.0

_DEFAULT_HINT = ("该插件当前无输出——若为催捐 / 授权 / 界面唤醒类插件，"
                 "请在插件界面里完成交互（如点击界面内按钮）后重新渲染。")


def _hint_from_map(m: dict | None) -> str | None:
    if not isinstance(m, dict):
        return None
    meta = m.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("notes"), str) and meta["notes"].strip():
        return meta["notes"].strip()
    notes = m.get("notes")
    if isinstance(notes, str) and notes.strip():
        return notes.strip()
    return None


def scan_silent_instruments(score, stem_peaks, *, floor_dbfs: float = SILENCE_FLOOR_DBFS) -> list[dict]:
    """扫描「有音符但全零」的 VST3 轨 → 结构化 warnings（按轨序）。

    `stem_peaks`：`{轨下标: 峰值}`（render_buses 的 stats["stem_peaks"]）。
    """
    floor = 10.0 ** (float(floor_dbfs) / 20.0)
    tracks = getattr(score, "tracks", None) or []
    out: list[dict] = []
    for key, peak in (stem_peaks or {}).items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(tracks)):
            continue
        tr = tracks[idx]
        inst = getattr(tr, "instrument", None)
        prog = str(getattr(inst, "program", "") or "")
        if not prog.startswith("vst3:"):
            continue
        n_notes = len(getattr(tr, "notes", None) or [])
        if n_notes == 0:
            continue
        try:
            pf = float(peak)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pf) or pf > floor:
            continue
        plugin = prog[len("vst3:"):]
        m = plugin_maps.load_map(plugin)
        display = str((m or {}).get("plugin") or Path(plugin).stem or plugin) if isinstance(m, dict) else \
            (Path(plugin).stem or plugin)
        out.append({
            "code": "instrument_silent",
            "track": idx,
            "name": tr.name or f"track-{idx}",
            "plugin": plugin,
            "plugin_name": display,
            "notes_count": n_notes,
            "peak": round(pf, 12),
            "hint": _hint_from_map(m) or _DEFAULT_HINT,
        })
    out.sort(key=lambda w: w["track"])
    return out


def format_warnings_text(warnings: list[dict]) -> str:
    """warnings → 可读多行文本（agent 工具输出用）。空列表 → 空串。"""
    if not warnings:
        return ""
    lines = [f"⚠ 插件静音提示（{len(warnings)} 轨「有音符但无输出」；可能需在插件界面交互后重渲）："]
    for w in warnings:
        lines.append(f"· {w['code']} track[{w['track']}] {w['name']}（{w['plugin_name']}，"
                     f"{w['notes_count']} 音符）：{w['hint']}")
    return "\n".join(lines)
