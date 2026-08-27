"""tsov.host —— 宿主框架（ADR-0013 + ADR-0015）：score → tracks → instruments + effects → audio。

- 渲染/回放：HostEngine / HostSession / SoundSource（SF2 实现，SFZ/VST3 预留）/ EffectProcessor（预留）
- 编辑命令层（M-V1，ADR-0015）：EditBatch 事务 / Project（undo-redo + git 版本 + 工程摘要）/ NoteDiff（音符级三色 diff）

与渲染层的分工：`tsov/render/` 是面对 M3 管线的最小渲染适配器（fluidsynth 文件渲染）；
`tsov/host/` 是产品内可交互的回放心脏 + 编辑真相（ADR-0003 DAW 心智落地），对外控制协议沿用 daw-cli 心智。
"""

from .command import BatchResult, EditBatch, EditCommand
from .device import mix_graph, play, write_wav
from .diff import NoteDiff, diff_notes
from .engine import HostEngine
from .effect import EffectProcessor
from .instrument import SF2Source, SFZSource, SoundSource, VST3Source, make_source
from .project import Project
from .session import HostSession, HostTrack

__all__ = [
    "HostEngine",
    "HostSession",
    "HostTrack",
    "Project",
    "EditBatch",
    "EditCommand",
    "BatchResult",
    "NoteDiff",
    "diff_notes",
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
