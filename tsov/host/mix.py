"""总线化混音（M-V4）：track → bus → master；volume/pan/mute/solo/automation 全应用。

设计要点：
- pan = 线性平衡律：L = clip(1-p)、R = clip(1+p)（对侧衰减、中位不衰减 → mono 折叠 (L+R)/2 无损）
- automation 数据在模型层（Track/Bus.automation），此处求值：线性插值、段外取端点值
- mute/solo：任一轨 solo → 仅 solo 轨可闻（mute 恒静音）
- 防削波：沿用旧 mix_graph 行为——full mix 峰值 >1 时整体缩放（`auto_scale=True` 缺省）
- 总线容错：track.bus 指向不存在的总线 → 按 master 处理
"""

from __future__ import annotations

import numpy as np

from ..core.score import Bus
from .effect import apply_effect_chain, effect_tail_seconds
from .session import HostSession


def _num(x, default: float) -> float:
    return float(default if x is None else x)


def _curve(points, times: np.ndarray) -> np.ndarray | None:
    """automation 点 [[t, v], …] → 逐帧曲线（线性插值；段外取端点值）；无点返回 None。"""
    if not points:
        return None
    pts = sorted((float(p[0]), float(p[1])) for p in points if p is not None and len(p) >= 2)
    if not pts:
        return None
    xs = np.array([p[0] for p in pts], dtype=np.float64)
    ys = np.array([p[1] for p in pts], dtype=np.float64)
    return np.interp(times, xs, ys, left=float(ys[0]), right=float(ys[-1]))


def _pan_gains(pan) -> tuple[np.ndarray, np.ndarray] | tuple[float, float]:
    """线性平衡律：中位 1/1；向一侧旋转 = 衰减对侧、同侧保持 1（不 boost）。"""
    if isinstance(pan, np.ndarray):
        return np.clip(1.0 - pan, 0.0, 1.0), np.clip(1.0 + pan, 0.0, 1.0)
    p = max(-1.0, min(1.0, float(pan)))
    return min(1.0, max(0.0, 1.0 - p)), min(1.0, max(0.0, 1.0 + p))


def _pan_matrix(lg, rg) -> np.ndarray:
    """pan 增益 → (2,) 或 (n, 2) 乘数矩阵（标量走广播、曲线走列堆叠）。"""
    if isinstance(lg, np.ndarray):
        return np.stack([lg, rg], axis=1).astype(np.float32)
    return np.array([lg, rg], dtype=np.float32)


def render_buses(
    session: HostSession,
    samplerate: int | None = None,
    stereo: bool = True,
    *,
    only_track: int | None = None,
    only_bus: str | None = None,
    include_bus_processing: bool = True,
    include_master_processing: bool = True,
    auto_scale: bool = True,
) -> np.ndarray:
    """总线化混音渲染。

    - `only_track` / `only_bus`：导出矩阵用（单轨 stem / 单总线 stem）
    - `include_bus_processing=False`：轨道处理后直进 master（stems：总线前）
    - `include_master_processing=False`：跳过 master 推子/pan（stems/buses：master 前）
    - `auto_scale`：峰值 >1 整体缩放（full mix 缺省行为；导出矩阵用 False + 统一缩放）
    - 返回 (n, 2) stereo；`stereo=False` 折叠为 mono (L+R)/2（与旧 mix_graph 数值一致）
    """
    samplerate = int(samplerate or session.samplerate)
    # 渲染长度 = 会话时长 + 1s 释放尾 + 效果链尾巴（reverb/delay；无效果时不改变既有长度）
    fx_tail = max(
        (effect_tail_seconds(getattr(ht.track.instrument, "effects", None)) for ht in session.tracks),
        default=0.0,
    )
    n_frames = max(1, int(round((session.duration + 1.0 + fx_tail) * samplerate)))
    times = np.arange(n_frames, dtype=np.float64) / samplerate

    master = np.zeros((n_frames, 2), dtype=np.float32)
    bus_defs: dict[str, Bus] = {b.name: b for b in (getattr(session.score, "buses", None) or [])}
    bus_bufs: dict[str, np.ndarray] = {}

    any_solo = any(bool(getattr(ht.track, "solo", False)) for ht in session.tracks)

    for idx, ht in enumerate(session.tracks):
        tr = ht.track
        if only_track is not None and idx != only_track:
            continue
        target = getattr(tr, "bus", "master") or "master"
        if only_bus is not None and target != only_bus:
            continue
        if bool(getattr(tr, "mute", False)) or (any_solo and not bool(getattr(tr, "solo", False))):
            continue

        buf = np.asarray(ht.source.render(ht.notes, samplerate, n_frames), dtype=np.float32)
        # 轨内处理：fader（Instrument.volume）→ automation 音量曲线 → pan
        buf = buf * _num(getattr(tr.instrument, "volume", 1.0), 1.0)
        auto = getattr(tr, "automation", None) or {}
        vol_curve = _curve(auto.get("volume"), times)
        if vol_curve is not None:
            buf = buf * vol_curve.astype(np.float32)
        pan_curve = _curve(auto.get("pan"), times)
        lg, rg = _pan_gains(pan_curve if pan_curve is not None else _num(getattr(tr, "pan", 0.0), 0.0))
        stereo_buf = np.stack([buf * lg, buf * rg], axis=1).astype(np.float32)

        # 效果链（ADR-0013 落地）：作用于 pan 后 stereo 缓冲（reverb 有真实立体声尾巴；
        # mute/solo/推子已先行，静音轨不会漏出残响）
        fx = getattr(tr.instrument, "effects", None) or []
        if fx:
            stereo_buf = apply_effect_chain(stereo_buf, samplerate, fx)

        if target == "master" or not include_bus_processing:
            master += stereo_buf
        else:
            bus_bufs.setdefault(target, np.zeros((n_frames, 2), dtype=np.float32))
            bus_bufs[target] += stereo_buf

    # 总线处理 → master
    for name, bb in bus_bufs.items():
        bdef = bus_defs.get(name)
        if bdef is not None:
            vol_curve = _curve((bdef.automation or {}).get("volume"), times)
            if vol_curve is not None:
                bb = bb * vol_curve[:, None].astype(np.float32)
            bb = bb * _num(bdef.volume, 1.0)
            pan_curve = _curve((bdef.automation or {}).get("pan"), times)
            lg, rg = _pan_gains(pan_curve if pan_curve is not None else _num(bdef.pan, 0.0))
            bb = bb * _pan_matrix(lg, rg)
        master += bb

    # master 处理
    mdef = getattr(session.score, "master", None) or Bus(name="master")
    if include_master_processing:
        vol_curve = _curve((mdef.automation or {}).get("volume"), times)
        if vol_curve is not None:
            master = master * vol_curve[:, None].astype(np.float32)
        master = master * _num(mdef.volume, 1.0)
        pan_curve = _curve((mdef.automation or {}).get("pan"), times)
        lg, rg = _pan_gains(pan_curve if pan_curve is not None else _num(mdef.pan, 0.0))
        master = master * _pan_matrix(lg, rg)

    if auto_scale and master.size:
        peak = float(np.max(np.abs(master)))
        if peak > 1.0:
            master = master / peak

    if stereo:
        return master
    return ((master[:, 0] + master[:, 1]) * 0.5).astype(np.float32)
