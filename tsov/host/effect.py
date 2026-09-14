"""效果器链（ADR-0013「插件标准兼容」接口层；效果链落地）。

- ``EffectProcessor``：音频处理接口（``process(audio) -> audio``），与音源解耦
- ``EffectChain``：基于 **pedalboard 内置效果器** 的具体实现
  （免费、离线、确定性；不引入第三方付费插件）
- ``apply_effect_chain``：混音层（host/mix）调用点
- ``effect_tail_seconds``：空间类效果尾巴估算（渲染长度补偿，导出矩阵一致性前提）

约定：
- 效果链在混音层作用于 **pan 后** 的 stereo 缓冲（让 reverb 有真实立体声尾巴；
  mute/solo/推子先于效果生效，静音轨不会漏出残响）
- 未知效果类型：明确报错（失败要响）；未知参数键：丢弃并记录到
  ``dropped_params``（``strict=True`` 时直接报错——预设校验用）
- 参数值按已知范围钳制；同一链重复处理同一输入 = 逐样本一致（无随机性）
"""

from __future__ import annotations

import abc

import numpy as np

from ..core.score import Effect

# ------------------------------------------------------------------
# 效果类型注册表：kind → (pedalboard 类名, 参数白名单[键 → 钳制范围/None])
# ------------------------------------------------------------------

_PARAM_RANGES: dict[str, dict[str, tuple[float, float] | None]] = {
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

_PLUGIN_NAMES: dict[str, str] = {
    "reverb": "Reverb", "delay": "Delay", "compressor": "Compressor", "chorus": "Chorus",
    "distortion": "Distortion", "gain": "Gain", "highpass": "HighpassFilter",
    "lowpass": "LowpassFilter", "limiter": "Limiter", "brickwall": "BrickwallLimiter",
    "phaser": "Phaser",
}


def effect_kinds() -> list[str]:
    """已支持的效果类型列表。"""
    return sorted(_PARAM_RANGES)


def _clamp(kind: str, key: str, value) -> float:
    rng = _PARAM_RANGES[kind].get(key)
    v = float(value)
    if rng is None:
        return v
    lo, hi = rng
    return float(min(hi, max(lo, v)))


def _plugin_class(kind: str):
    import pedalboard

    name = _PLUGIN_NAMES.get(kind)
    if name is None or not hasattr(pedalboard, name):
        raise ValueError(
            f"不支持的效果类型：{kind!r}（可用：{', '.join(effect_kinds())}）"
        )
    return getattr(pedalboard, name)


def validate_effect(effect: Effect) -> list[str]:
    """校验单个效果：返回问题列表（空 = 通过）。供预设入库检查用。"""
    problems: list[str] = []
    kind = getattr(effect, "type", None)
    if kind not in _PARAM_RANGES:
        problems.append(f"未知效果类型：{kind!r}")
        return problems
    for key in (effect.params or {}):
        if key not in _PARAM_RANGES[kind]:
            problems.append(f"{kind}: 未知参数键 {key!r}")
    return problems


class EffectProcessor(abc.ABC):
    """效果器处理器接口：音频 in → 音频 out（保留采样率与帧数）。"""

    name: str = "abstract"

    @abc.abstractmethod
    def process(self, audio: np.ndarray, samplerate: int) -> np.ndarray:
        """（mono/stereo float32）→ 处理后的音频（同形）。"""


class EffectChain(EffectProcessor):
    """pedalboard 内置效果器的串联链（按列表顺序处理）。

    ``process`` 接受 ``(n,)`` mono 或 ``(n, 2)`` 立体声，返回同形；
    内部按 pedalboard 约定转为 ``(channels, n)`` 再处理。
    """

    name = "pedalboard-chain"

    def __init__(self, effects: list[Effect], *, strict: bool = False):
        self.effects = list(effects)
        self.dropped_params: list[str] = []
        self._plugins: list = []
        for e in self.effects:
            kind = getattr(e, "type", None)
            if kind not in _PARAM_RANGES:
                raise ValueError(
                    f"不支持的效果类型：{kind!r}（可用：{', '.join(effect_kinds())}）"
                )
            params: dict[str, float] = {}
            for key, value in (getattr(e, "params", None) or {}).items():
                if key not in _PARAM_RANGES[kind]:
                    if strict:
                        raise ValueError(f"{kind}: 未知参数键 {key!r}")
                    self.dropped_params.append(f"{kind}.{key}")
                    continue
                params[key] = _clamp(kind, key, value)
            self._plugins.append(_plugin_class(kind)(**params))

    def process(self, audio: np.ndarray, samplerate: int) -> np.ndarray:
        arr = np.ascontiguousarray(np.asarray(audio, dtype=np.float32))
        if arr.ndim == 1:
            out = arr[None, :]
            for plugin in self._plugins:
                out = plugin.process(out, samplerate)
            return np.ascontiguousarray(out[0], dtype=np.float32)
        if arr.ndim == 2:
            ch, n = arr.shape
            out = np.ascontiguousarray(arr.T, dtype=np.float32)
            for plugin in self._plugins:
                out = plugin.process(out, samplerate)
            return np.ascontiguousarray(out.T, dtype=np.float32)
        raise ValueError(f"不支持的音频形状：{arr.shape}")


def apply_effect_chain(
    audio: np.ndarray, samplerate: int, effects: list[Effect] | None
) -> np.ndarray:
    """便捷入口：空链原样返回（零拷贝）；否则按顺序处理。"""
    if not effects:
        return audio
    return EffectChain(list(effects)).process(np.asarray(audio, dtype=np.float32), samplerate)


def effect_tail_seconds(effects: list[Effect] | None) -> float:
    """估算空间类效果的尾巴时长（渲染长度补偿；非空间类返回 0）。

    reverb：``1 + 4*room_size`` 经验近似；delay：按反馈次数估算（封顶 8s）。
    """
    tail = 0.0
    for e in effects or []:
        kind = getattr(e, "type", None)
        p = getattr(e, "params", None) or {}
        if kind == "reverb":
            room = _clamp("reverb", "room_size", p.get("room_size", 0.5))
            tail = max(tail, 1.0 + 4.0 * room)
        elif kind == "delay":
            d = _clamp("delay", "delay_seconds", p.get("delay_seconds", 0.0))
            fb = _clamp("delay", "feedback", p.get("feedback", 0.0))
            tail = max(tail, min(8.0, d * (1.0 + 10.0 * fb)))
    return float(min(8.0, tail))
