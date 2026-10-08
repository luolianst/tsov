"""总线化混音（M-V4 / ADR-0018 换序）：track → bus → master；volume/pan/mute/solo/automation 全应用。

轨内管线（ADR-0018，2026-09-20 换序为 DAW 标准序）：
    **音源 → 效果链 → 推子 → automation 音量 → 声像** → 总线 → master
- 前段（音源+效果）＝ 可缓存部分（轨道级 freeze，`cache.StemStore`）；后段（推子/自动化/声像）**混音期实时应用、永不触发重渲**
- pan = 线性平衡律：L = clip(1-p)、R = clip(1+p)（对侧衰减、中位不衰减 → mono 折叠 (L+R)/2 无损）
- automation 数据在模型层（Track/Bus.automation），此处求值：线性插值、段外取端点值
- mute/solo：任一轨 solo → 仅 solo 轨可闻（mute 恒静音）
- 防削波：沿用旧 mix_graph 行为——full mix 峰值 >1 时整体缩放（`auto_scale=True` 缺省）
- 总线容错：track.bus 指向不存在的总线 → 按 master 处理
- M-V8 E5：**send 支路**——post-fader（推子+automation+声像后）× 量 → 汇入目标总线缓冲（与分组路由同缓冲，
  总线处理照常作用）；**stems 导出（only_track）不含 send 支路**；总线/master 处理序升级为
  **效果 → 音量/automation → 声像**（与轨内同构；无效果时逐位与旧版一致）

（换序前后差异只出现在「非线性效果 + 非单位推子」的轨：新序下效果输入不再被推子驱动；
旧序产物对照见 `output/mv7-d1-compare/`，任务书 D1 闸门 1。）
"""

from __future__ import annotations

import numpy as np

from ..core.score import Bus
from .audio_source import AudioClipSource
from .cache import StemStore
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


# ---- v0.2 批C2（F5）求值分域（设计件 §3.2）----
# mix 域 = **乘性级联**：底值 × 各层曲线值（v1 层序：轨道道 ×〔F6 接入文件夹层〕…）；
# perf 域（预留：CC/弯音等）= 按 kind 合并表——缺省 "last_continue"（末值延续，= Replace2 语义）；
#   "average" / "modulation" 留表位（P46，本批不实现）。
# combine 口：道结构预留 combine="absolute" 缺省字段（他档不实现，本批不动数据）。
PERF_MERGE_DEFAULT = "last_continue"
PERF_MERGE_TABLE: dict[str, str] = {}   # kind → merge（空表 = 全部走缺省 last_continue）


def active_automation(tr) -> dict:
    """automation 求值范围（v0.2 批C2 求值分域）：

    - lanes 未管理（None）→ automation 全部键（旧工程同构）；
    - lanes 已物化（含空列表）→ 只取 lanes 内 param 的曲线（空列表 = 无道 = 全部失活；
      曲线数据仍保留在 dict，非破坏——重新绑回即复活）。"""
    auto = getattr(tr, "automation", None) or {}
    lanes = getattr(tr, "lanes", None)
    if lanes is None:
        return dict(auto)
    allowed = {str(l.get("param")) for l in lanes if isinstance(l, dict)}
    return {k: v for k, v in auto.items() if str(k) in allowed}


def render_track_source(ht, samplerate: int, n_frames: int) -> np.ndarray:
    """轨内**前段**（可缓存，ADR-0018）：音源合成 → 效果链（居中双单声道输入）。

    返回 (n_frames, 2) float32；不含推子/自动化/声像（这些由 `apply_track_mix` 在混音期应用）。

    M-V8 E6：音源返回 (n,) mono 时按既有口径复制双声道；返回 (n,2)（如音频轨，
    立体声保真）时直接用其 L/R（>2 声道取前两路）。
    """
    buf = np.asarray(ht.source.render(ht.notes, samplerate, n_frames), dtype=np.float32)
    if buf.ndim == 1:
        stereo = np.stack([buf, buf], axis=1).astype(np.float32)
    elif buf.shape[1] == 1:
        stereo = np.repeat(buf, 2, axis=1).astype(np.float32)
    else:
        stereo = np.ascontiguousarray(buf[:, :2], dtype=np.float32)
    fx = getattr(ht.track.instrument, "effects", None) or []
    if fx:
        stereo = apply_effect_chain(stereo, samplerate, fx)
    return stereo


def apply_track_mix(stereo: np.ndarray, tr, times: np.ndarray) -> np.ndarray:
    """轨内**后段**（混音期实时层，永不失效）：推子 → automation 音量 → 声像（平衡律）。

    v0.2 批C2（F5 求值分域）：automation 求值范围 = `active_automation(tr)`——lanes 已物化时
    只取道内参数（未管理 = 全键，旧工程同构）；mix 域为**乘性级联**（各层曲线逐帧相乘；
    v1 层 = 轨道道 ×〔F6 接文件夹层〕）。无自动化数据时逐位与旧口径一致。"""
    out = stereo * _num(getattr(tr.instrument, "volume", 1.0), 1.0)
    auto = active_automation(tr)
    vol_curve = _curve(auto.get("volume"), times)
    if vol_curve is not None:
        out = out * vol_curve[:, None].astype(np.float32)
    pan_curve = _curve(auto.get("pan"), times)
    lg, rg = _pan_gains(pan_curve if pan_curve is not None else _num(getattr(tr, "pan", 0.0), 0.0))
    return out * _pan_matrix(lg, rg)


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
    cache: StemStore | None = None,
    stats: dict | None = None,
    on_progress=None,
) -> np.ndarray:
    """总线化混音渲染。

    - `only_track` / `only_bus`：导出矩阵用（单轨 stem / 单总线 stem）
    - `include_bus_processing=False`：轨道处理后直进 master（stems：总线前）
    - `include_master_processing=False`：跳过 master 推子/pan（stems/buses：master 前）
    - `auto_scale`：峰值 >1 整体缩放（full mix 缺省行为；导出矩阵用 False + 统一缩放）
    - `cache`（ADR-0018）：轨道级 freeze 库——前段命中即读、未命中渲后入库
    - `stats` / `on_progress`：缓存统计与逐轨进度回调（web 层用于 SSE 进度）
    - 返回 (n, 2) stereo；`stereo=False` 折叠为 mono (L+R)/2（与旧 mix_graph 数值一致）
    """
    samplerate = int(samplerate or session.samplerate)
    bus_defs: dict[str, Bus] = {b.name: b for b in (getattr(session.score, "buses", None) or [])}
    mdef = getattr(session.score, "master", None) or Bus(name="master")
    # 渲染长度 = 会话时长 + 1s 释放尾 + 效果链尾巴（reverb/delay；无效果时不改变既有长度）
    # M-V8 E5：尾巴汇总纳入总线/master 效果链（取最大）
    fx_tail = max(
        [effect_tail_seconds(getattr(ht.track.instrument, "effects", None)) for ht in session.tracks]
        + [effect_tail_seconds(getattr(b, "effects", None)) for b in bus_defs.values()]
        + [effect_tail_seconds(getattr(mdef, "effects", None))],
        default=0.0,
    )
    n_frames = max(1, int(round((session.duration + 1.0 + fx_tail) * samplerate)))
    times = np.arange(n_frames, dtype=np.float64) / samplerate

    master = np.zeros((n_frames, 2), dtype=np.float32)
    bus_bufs: dict[str, np.ndarray] = {}

    any_solo = any(bool(getattr(ht.track, "solo", False)) for ht in session.tracks)

    send_on = include_bus_processing and only_track is None   # stems（单轨）不含 send 支路
    for idx, ht in enumerate(session.tracks):
        tr = ht.track
        if only_track is not None and idx != only_track:
            continue
        target = getattr(tr, "bus", "master") or "master"
        # M-V8 E5：send 支路集合（量>0；only_bus 导出时也检查汇入本总线的 send）
        sends_here: dict[str, float] = {}
        if send_on:
            for sname, amt in (getattr(tr, "sends", None) or {}).items():
                a = _num(amt, 0.0)
                if a > 0:
                    sends_here[str(sname)] = a
        if only_bus is not None and target != only_bus and only_bus not in sends_here:
            continue
        if bool(getattr(tr, "mute", False)) or (any_solo and not bool(getattr(tr, "solo", False))):
            continue

        name = tr.name or f"track-{idx}"
        # 轨内前段（音源+效果）：ADR-0018 缓存路径（命中即读；未命中渲后入库）
        # E2：音频轨不走 stem 缓存（文件直读——入库素材不变；轨道级缓存留后续）
        if cache is not None and not isinstance(ht.source, AudioClipSource):
            key = cache.key_for(tr, samplerate)
            buf = cache.load(key)
            if buf is None:
                if on_progress:
                    on_progress({"idx": idx, "name": name, "state": "render"})
                buf = render_track_source(ht, samplerate, n_frames)
                cache.save(key, buf, samplerate)
                if stats is not None:
                    stats.setdefault("rendered", []).append(name)
            else:
                if stats is not None:
                    stats.setdefault("cached", []).append(name)
                if on_progress:
                    on_progress({"idx": idx, "name": name, "state": "cached"})
            if buf.shape[0] != n_frames:   # 工程时长变化 → 补齐/裁剪（零填充：该轨内容未变形）
                if buf.shape[0] < n_frames:
                    buf = np.pad(buf, ((0, n_frames - buf.shape[0]), (0, 0)))
                else:
                    buf = buf[:n_frames]
        else:
            buf = render_track_source(ht, samplerate, n_frames)

        # 轨内后段（混音期实时层）：推子 → automation → 声像
        stereo_buf = apply_track_mix(np.asarray(buf, dtype=np.float32), tr, times)

        if only_bus is None or target == only_bus:
            if target == "master" or not include_bus_processing:
                master += stereo_buf
            else:
                bus_bufs.setdefault(target, np.zeros((n_frames, 2), dtype=np.float32))
                bus_bufs[target] += stereo_buf
        # M-V8 E5：send 支路（post-fader × 量；与分组路由同缓冲；总线处理照常作用）
        for sname, amt in sends_here.items():
            if only_bus is not None and sname != only_bus:
                continue
            contrib = stereo_buf * amt
            if sname == "master":
                master += contrib
            else:
                bus_bufs.setdefault(sname, np.zeros((n_frames, 2), dtype=np.float32))
                bus_bufs[sname] += contrib

    # 总线处理（效果 → 音量/automation → 声像，与轨内同构）→ master
    for name, bb in bus_bufs.items():
        bdef = bus_defs.get(name)
        if bdef is not None:
            fx = getattr(bdef, "effects", None) or []
            if fx:
                bb = apply_effect_chain(np.asarray(bb, dtype=np.float32), samplerate, fx)
            vol_curve = _curve((bdef.automation or {}).get("volume"), times)
            if vol_curve is not None:
                bb = bb * vol_curve[:, None].astype(np.float32)
            bb = bb * _num(bdef.volume, 1.0)
            pan_curve = _curve((bdef.automation or {}).get("pan"), times)
            lg, rg = _pan_gains(pan_curve if pan_curve is not None else _num(bdef.pan, 0.0))
            bb = bb * _pan_matrix(lg, rg)
        master += bb

    # master 处理（效果 → 音量/automation → 声像）
    if include_master_processing:
        fx = getattr(mdef, "effects", None) or []
        if fx:
            master = apply_effect_chain(np.asarray(master, dtype=np.float32), samplerate, fx)
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
