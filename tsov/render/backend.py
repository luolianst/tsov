"""渲染层后端适配器接口（ADR-0003 DAW 心智抽象）。

第一版只实现 fluidsynth 后端；接口按终局形态一次定对（Score → 多轨 audio）。
"""

from __future__ import annotations

import abc

from ..core.score import Score

_REGISTRY: dict[str, "Backend"] = {}


class Backend(abc.ABC):
    """渲染后端适配器基类：Score → audio。"""

    name: str = "abstract"

    @abc.abstractmethod
    def render(self, score: Score, output_path: str, **params) -> str:
        """把 Score 渲染成音频文件，返回产物路径。"""

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name != "abstract":
            _REGISTRY[cls.name] = cls()


def render_score(score: Score, backend: str = "fluidsynth", output_path: str = "out.wav", **params) -> str:
    """按后端名渲染 Score → 音频文件。"""
    return get_backend(backend).render(score, output_path, **params)


def render_midi(midi_path: str, output_path: str, soundfont: str | None = None, **params) -> str:
    """MIDI 文件 → WAV（fluidsynth 后端能力；CLI/管线直接对 .mid 用）。"""
    from .fluidsynth_backend import render_midi as _render_midi

    return _render_midi(midi_path, output_path, soundfont=soundfont, **params)


def get_backend(name: str) -> Backend:
    if name not in _REGISTRY:
        raise ValueError(f"未知渲染后端：{name!r}（可选 {list_backends()}）")
    return _REGISTRY[name]


def list_backends() -> list[str]:
    return sorted(_REGISTRY)
