"""tsov.host —— 宿主框架（ADR-0013 + ADR-0015）：score → tracks → instruments + effects → audio。

- 渲染/回放：HostEngine / HostSession / SoundSource（SF2/SFZ/VST3 实现）
- 效果链（ADR-0013 落地）：effect.EffectChain（pedalboard 内置效果；混音层 pan 后应用）
- 录制（host 层）：record.record_to_wav（输入设备 → WAV；CLI `tsov host record`）
- 混音/总线（M-V4）：mix.render_buses（track → bus → master，volume/pan/mute/solo/automation）
- 导出矩阵（M-V4）：export.export_matrix（master/bus/stems WAV + MIDI）
- 编辑命令层（M-V1，ADR-0015）：EditBatch 事务 / Project（undo-redo + git 版本 + 工程摘要）/ NoteDiff（音符级三色 diff）

与渲染层的分工：`tsov/render/` 是面对 M3 管线的最小渲染适配器（fluidsynth 文件渲染）；
`tsov/host/` 是产品内可交互的回放心脏 + 编辑真相（ADR-0003 DAW 心智落地），对外控制协议沿用 daw-cli 心智。
"""

from .command import BatchResult, EditBatch, EditCommand
from .device import mix_graph, play, write_wav
from .diff import NoteDiff, diff_notes
from .engine import HostEngine
from .effect import EffectChain, EffectProcessor, apply_effect_chain, effect_kinds, effect_tail_seconds, validate_effect
from .export import export_matrix
from .instrument import SF2Source, SFZSource, SoundSource, VST3Source, make_source
from .mix import render_buses
from .project import Project
from .record import list_input_devices, record_to_wav
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
    "EffectChain",
    "apply_effect_chain",
    "effect_kinds",
    "effect_tail_seconds",
    "validate_effect",
    "record_to_wav",
    "list_input_devices",
    "make_source",
    "mix_graph",
    "render_buses",
    "export_matrix",
    "play",
    "write_wav",
]
