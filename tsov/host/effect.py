"""效果器接口（ADR-0013「插件标准兼容」接口层）。

- EffectProcessor 抽象：process(audio) -> audio，与音源解耦（纯音频处理链）
- 插件标准：VST3 / CLAP（接口预留，第一版无实现；底座候选 pedalboard / popsicle / clap-host）
- Instrument.effects 目前为空列表（ADR-0005 schema 预留的扩展点）；effect 链接入放在里程碑二
"""

from __future__ import annotations

import abc

import numpy as np


class EffectProcessor(abc.ABC):
    """效果器处理器接口：音频 in → 音频 out（保留采样率与帧数）。"""

    name: str = "abstract"

    @abc.abstractmethod
    def process(self, audio: np.ndarray, samplerate: int) -> np.ndarray:
        """（mono/stereo float32）→ 处理后的音频（同形）。"""
