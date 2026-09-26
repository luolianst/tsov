"""电平测量（M-V8 E4 段2 · Q2）：analyze_levels 口径的 schema 版。

口径（与 agent 工具 analyze_levels 同源，输出结构化）：
- 每轨：render_buses(only_track=i, auto_scale=False) → 整段 RMS + 最响窗（非重叠窗取最大）
- 混音峰值：full mix 渲染（auto_scale=False）→ 线性峰值（>1.0 = 会削波/被整体缩放）
- 相对表锚点 = 整段 RMS 最响轨（0 dB）
- 逐段电平：直接切 stem 缓冲段窗（事实包「全曲 + 逐段」范围的段口径）

LUFS：ffmpeg loudnorm 量档（外部工具缺失/失败 → None，不让调参链挂）。
纯数学零 IO 例外：measure_lufs 调 ffmpeg。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import numpy as np

from .spectrum import mono

DB_FLOOR = -120.0


def db(x: float) -> float:
    """线性 → dBFS（x ≤ 0 → 地板 -120；与 analyze_levels 的 db() 同式）。"""
    return round(20.0 * float(np.log10(max(float(x), 1e-9))), 2) if x else DB_FLOOR


def rms_dbfs(audio) -> float:
    a = mono(audio)
    if a.size == 0:
        return DB_FLOOR
    return db(float(np.sqrt(np.mean(a ** 2))))


def peak_dbfs(audio) -> float:
    a = np.asarray(audio)
    if a.size == 0:
        return DB_FLOOR
    return db(float(np.max(np.abs(a))))


def loudest_window(audio, samplerate: int, window_sec: float = 4.0) -> tuple[float, float]:
    """最响窗（非重叠窗取最大）→ (dBFS, 起点秒)。"""
    a = mono(audio)
    if a.size == 0:
        return DB_FLOOR, 0.0
    w = max(1, int(window_sec * samplerate))
    n_win = max(1, a.size // w)
    best, at = DB_FLOOR, 0.0
    for k in range(n_win):
        seg = a[k * w:(k + 1) * w]
        if seg.size == 0:
            continue
        r = float(np.sqrt(np.mean(seg ** 2)))
        if r > 0 and db(r) > best:
            best, at = db(r), k * w / float(samplerate)
    return best, round(at, 2)


def section_rms_dbfs(audio, samplerate: int, start: float, end: float) -> float:
    """段窗 RMS（起止秒；越界自动截断）。"""
    a = mono(audio)
    i0 = max(0, int(float(start) * samplerate))
    i1 = min(a.size, int(float(end) * samplerate))
    if i1 <= i0:
        return DB_FLOOR
    return rms_dbfs(a[i0:i1])


def track_metrics(audio, samplerate: int, *, window_sec: float = 4.0) -> dict:
    """单轨指标（schema）：整段 RMS / 最响窗（dB + 起点）/ 峰值 / 时长 / 是否静音。"""
    a = mono(audio)
    if a.size == 0:
        return {"silent": True, "duration_sec": 0.0, "rms_dbfs": DB_FLOOR,
                "loudest_win_dbfs": DB_FLOOR, "loudest_win_at": 0.0, "peak_dbfs": DB_FLOOR}
    loud_db, at = loudest_window(a, samplerate, window_sec)
    return {
        "silent": bool(float(np.max(np.abs(a))) <= 1e-6),
        "duration_sec": round(a.size / float(samplerate), 2),
        "rms_dbfs": rms_dbfs(a),
        "loudest_win_dbfs": loud_db,
        "loudest_win_at": at,
        "peak_dbfs": peak_dbfs(a),
    }


def mix_peak(audio) -> tuple[float, bool]:
    """full mix 峰值 → (dBFS, 是否超满刻度会削波)。"""
    a = np.asarray(audio)
    if a.size == 0:
        return DB_FLOOR, False
    pk = float(np.max(np.abs(a)))
    return db(pk), bool(pk > 1.0)


def measure_lufs(wav_path, *, timeout: float = 120.0) -> float | None:
    """ffmpeg loudnorm 量档 → 集成响度（LUFS）；工具缺失/失败 → None（不让调参链挂）。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-nostats", "-i", str(wav_path),
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        m = re.search(r"\{\s*\"input_i\".*?\}", proc.stderr or "", re.S)
        if not m:
            return None
        return round(float(json.loads(m.group(0))["input_i"]), 1)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None
