"""tsov.host —— 宿主框架（ADR-0013）：score → tracks → instruments + effects → audio。

毛胚实现 + 接口完整预留：
- SoundSource：音源适配器接口（SF2 ✅ / SFZ、VST3 接口预留）
- EffectProcessor：效果器接口（VST3/CLAP 预留，第一版无实现）
- HostSession / HostEngine：transport + 实时回放（预渲染缓冲 + sounddevice）与离线渲染同一 graph

与渲染层的分工：`tsov/render/` 是面对 M3 管线的最小渲染适配器（fluidsynth 文件渲染）；
`tsov/host/` 是产品内可交互的回放心脏（ADR-0003 DAW 心智落地），对外控制协议沿用 daw-cli 心智。
"""

from .device import mix_graph, play, write_wav
from .engine import HostEngine
from .effect import EffectProcessor
from .instrument import SF2Source, SFZSource, SoundSource, VST3Source, make_source
from .session import HostSession, HostTrack

__all__ = [
    "HostEngine",
    "HostSession",
    "HostTrack",
    "SoundSource",
    "SF2Source",
    "VST3Source",
    "SFZSource",
    "EffectProcessor",
    "make_source",
    "mix_graph",
    "play",
    "write_wav",
]
