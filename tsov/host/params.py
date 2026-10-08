"""参数注册表（v0.2 批C 前段·地基件 R1；设计件 `docs/v0.2-批C-前段地基件-设计-2026-10-08.md` §二）。

单一来源：把散在多处的同一份参数知识收拢于此——
- 自动化域（mix）：id / 域 / 标签 / 范围 / 求值语义 / 可自动化标志（``ParamSpec``）
- 效果域：kind → {参数键 → 钳制范围 | None}（形状与历史 ``effect._PARAM_RANGES`` 完全一致）

消费方：``host.command``（自动化校验/钳制）· ``host.effect``（效果域薄壳）·
``tune.suggest``（调参上下文）· ``webapp /api/meta``（前端快照）。

扩展程序（设计件 §3.2，后续批一律遵守）：
① 一切扩展 additive + 缺省旧数据同构；② 新参数先在此登记（id/域/范围/求值）；
③ 读写必走命令层 op；④ 需新字段（非 dict 键）→ 先写 ADR 再动。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParamSpec:
    """单个可寻址参数的注册条目（id 一旦公开即成稳定寻址——只增不改）。"""

    id: str
    domain: str            # "mix" | "effect" |（预留 "perf" = CC/弯音）
    label: str             # 中文显示名（UI 用）
    lo: float | None = None
    hi: float | None = None
    default: float | None = None
    unit: str = ""         # "%" / "Hz" / "dB" …（显示层换算；空 = 原样）
    automatable: bool = False   # 可否进 set_automation / 道（曲线类）
    eval_kind: str = ""         # 求值语义："curve"（mix 曲线）| "plugin"（效果）| "midi"（预留）


# ------------------------------------------------------------------
# 自动化域（mix）
# 顺序 = 对外展示/错误文案顺序（volume → pan）；数值与历史 _AUTOMATION_PARAMS 一字不差。
# ------------------------------------------------------------------

_MIX_SPECS: dict[str, ParamSpec] = {
    "volume": ParamSpec("volume", "mix", "音量", 0.0, 2.0, 1.0, "%", True, "curve"),
    "pan": ParamSpec("pan", "mix", "声像", -1.0, 1.0, 0.0, "", True, "curve"),
}


# ------------------------------------------------------------------
# 效果域（自 host/effect.py 迁入；形状 = kind → {键: (lo, hi) | None}）
# ------------------------------------------------------------------

_EFFECT_RANGES: dict[str, dict[str, tuple[float, float] | None]] = {
    "reverb": {
        "room_size": (0.0, 1.0), "damping": (0.0, 1.0), "wet_level": (0.0, 1.0),
        "dry_level": (0.0, 1.0), "width": (0.0, 1.0), "freeze_mode": (0.0, 1.0),
    },
    "delay": {"delay_seconds": (0.0, 10.0), "feedback": (0.0, 1.0), "mix": (0.0, 1.0)},
    "compressor": {
        "threshold_db": (-60.0, 0.0), "ratio": (1.0, 20.0),
        "attack_ms": (0.1, 100.0), "release_ms": (5.0, 2000.0),
    },
    "chorus": {
        "rate_hz": (0.0, 20.0), "depth": (0.0, 1.0), "centre_delay_ms": (0.0, 50.0),
        "feedback": (0.0, 1.0), "mix": (0.0, 1.0),
    },
    "distortion": {"drive_db": (0.0, 60.0)},
    "gain": {"gain_db": (-60.0, 24.0)},
    "highpass": {"cutoff_frequency_hz": (10.0, 20000.0)},
    "lowpass": {"cutoff_frequency_hz": (10.0, 20000.0)},
    "limiter": {"threshold_db": (-60.0, 0.0), "release_ms": (0.1, 5000.0)},
    "brickwall": {"ceiling_db": (-60.0, 0.0), "release_ms": (0.1, 5000.0)},
    "phaser": {
        "rate_hz": (0.0, 10.0), "depth": (0.0, 1.0), "centre_frequency_hz": (20.0, 20000.0),
        "feedback": (0.0, 1.0), "mix": (0.0, 1.0),
    },
}


def automation_specs() -> dict[str, ParamSpec]:
    """自动化域参数表（只读快照；顺序稳定 = volume → pan）。"""
    return dict(_MIX_SPECS)


def effect_param_ranges() -> dict[str, dict[str, tuple[float, float] | None]]:
    """效果域参数范围表（只读快照；形状与历史 effect._PARAM_RANGES 完全一致）。"""
    return _EFFECT_RANGES


def snapshot() -> dict:
    """注册表只读快照（JSON 形状）：供 /api/meta 与 tune 上下文共用。"""
    return {
        "automation": {
            pid: {"label": s.label, "lo": s.lo, "hi": s.hi, "unit": s.unit, "automatable": s.automatable}
            for pid, s in _MIX_SPECS.items()
        },
        "effects": {
            k: {kk: (list(vv) if vv else None) for kk, vv in v.items()}
            for k, v in _EFFECT_RANGES.items()
        },
    }
