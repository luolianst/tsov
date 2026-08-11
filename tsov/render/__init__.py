"""tsov.render —— 渲染层（DAW 心智抽象，ADR-0003 决策 4）。

score → tracks → instruments + effects → audio + 后端适配器接口。
fluidsynth 是第一适配器（回放音色中性）；Kontakt/合成器/DAW 宿主为后续适配器。
"""

from .backend import Backend, get_backend, list_backends, render_midi, render_score
from . import fluidsynth_backend  # noqa: F401 导入即注册到后端注册表

__all__ = ["Backend", "render_score", "render_midi", "get_backend", "list_backends"]
