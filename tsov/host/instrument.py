"""音源适配器（ADR-0013「音源标准兼容」接口层）。

标准兼容清单：
- SF2/SF3：pyfluidsynth（初版实现 `SF2Source`，实时流式渲染，非文件 midi2audio）
- SFZ：接口预留（`SFZSource` 桩）
- VST3：接口预留（`VST3Source` 桩；实现底座候选 pedalboard——实测可加载 VST3 并对乐器发 MIDI、popsicle 备胎）
- GM program：复用 `tsov/midi/export.GM_PROGRAMS`；MIDI 1.0 音符事件（start/end/pitch/velocity）直接消费

接口契约：`render(notes, samplerate, n_frames) -> mono float32 [-1,1]`。
换音源 = 新写一个 SoundSource 子类，宿主/上层零改动。
"""

from __future__ import annotations

import abc
from typing import Iterable

import numpy as np

from ..core.notes import Note
from ..midi.export import GM_PROGRAMS, is_drum_track, program_number  # noqa: F401  约定即文档


class SoundSource(abc.ABC):
    """音源接口：音符序列 → 音频缓冲。

    - 每轨一个实例（毛胚规模）；volume 由混合层（device.mix_graph）统一应用
    - 缺省输出 mono float32（多轨混音层面再按 Instrument.volume 加权）
    """

    name: str = "abstract"

    @abc.abstractmethod
    def render(self, notes: Iterable[Note], samplerate: int, n_frames: int) -> np.ndarray:
        """音符序列 → mono float32（长度 n_frames，不足补零）。"""

    def close(self) -> None:
        """释放底层资源（synth/句柄），引擎 shutdown 时调用。"""


class SF2Source(SoundSource):
    """SF2/SF3 SoundFont 音源（pyfluidsynth 实时合成，初版实现）。

    - 每个源一个 Synth 实例（get_samples 直接拉流，不走 midi2audio 文件渲染）
    - 通道策略：打击乐 → channel 9（GM percussion），其余 → channel 0
    - 多轨体积大了再共享 engine（接口已隔离，只换实现）
    """

    name = "sf2"

    def __init__(self, soundfont: str, program: str = "piano", gain: float = 0.8, samplerate: int = 44100):
        from ..render.fluidsynth_backend import _load_fluidsynth

        self._fluid = _load_fluidsynth()
        self._samplerate = int(samplerate)
        self._synth = self._fluid.Synth(gain=gain, samplerate=self._samplerate)
        self._sfid = self._synth.sfload(str(soundfont))
        if self._sfid == self._fluid.FLUID_FAILED:
            self._synth.delete()
            raise RuntimeError(f"SoundFont 加载失败：{soundfont}")
        self.program = program
        self._channel = 9 if is_drum_track(program) else 0
        self._synth.program_select(self._channel, self._sfid, 0, program_number(program))

    def render(self, notes: Iterable[Note], samplerate: int, n_frames: int) -> np.ndarray:
        notes = list(notes)
        events: list[tuple[float, int, Note]] = []
        for n in notes:
            events.append((float(n.start), 1, n))
            events.append((float(n.end), 0, n))
        events.sort(key=lambda e: e[0])

        out = np.zeros((n_frames,), dtype=np.float32)
        pos = 0
        for t, kind, n in events:
            i = min(n_frames, max(pos, int(round(t * samplerate))))
            if i > pos:
                out[pos:i] += self._pull(i - pos)
                pos = i
            if kind == 1:
                vel = int(round(127.0 * min(1.0, max(0.0, float(n.velocity)))))
                self._synth.noteon(self._channel, int(n.pitch_midi), vel)
            else:
                self._synth.noteoff(self._channel, int(n.pitch_midi))
        if pos < n_frames:
            out[pos:] += self._pull(n_frames - pos)
        return out

    def _pull(self, n_frames: int) -> np.ndarray:
        """从 synth 拉 n_frames 帧（get_samples 返回 2n 个 int16 立体声交织）→ mono。"""
        buf = np.asarray(self._synth.get_samples(max(1, int(n_frames))), dtype=np.float32) / 32768.0
        buf = buf.reshape(-1, 2)
        return buf.mean(axis=1)

    def close(self) -> None:
        self._synth.delete()


class VST3Source(SoundSource):
    """VST3 音源接口预留（毛胚桩，ADR-0013）。

    实现方向（里程碑二）：pedalboard 加载 VST3 乐器 + MIDI 事件喂入（社区已有实测先例），
    popsicle（JUCE Python 绑定）备胎。接口已冻结：`render(notes→audio)` 与 SF2Source 一致，
    届时只换实现不换上层。
    """

    name = "vst3"

    def __init__(self, plugin_path: str, program: str = "", **params):  # pragma: no cover
        raise NotImplementedError(
            "VST3Source 接口预留（ADR-0013）：底座候选 pedalboard / popsicle，里程碑二实现。"
            f"plugin_path={plugin_path!r}"
        )

    def render(self, notes, samplerate, n_frames):  # pragma: no cover
        raise NotImplementedError("VST3Source 未实现")


class SFZSource(SoundSource):
    """SFZ 采样器音源接口预留（毛胚桩，ADR-0013）。"""

    name = "sfz"

    def __init__(self, sfz_path: str, **params):  # pragma: no cover
        raise NotImplementedError(f"SFZ 接口预留（sfz 采样器适配器，里程碑二实现）。sfz_path={sfz_path!r}")

    def render(self, notes, samplerate, n_frames):  # pragma: no cover
        raise NotImplementedError("SFZSource 未实现")


def make_source(program: str, soundfont: str | None = None, **params) -> SoundSource:
    """按 Instrument.program 构造音源（工厂）。

    第一版只有 sf2（fluidsynth）；program 形如 "vst3:<path>" / "sfz:<path>" 时走对应桩。
    """
    if program.startswith("vst3:"):
        return VST3Source(program[len("vst3:") :], **params)
    if program.startswith("sfz:"):
        return SFZSource(program[len("sfz:") :], **params)
    if not soundfont:
        raise RuntimeError("SF2 音源需要 soundfont 路径（make_source(soundfont=...) 或 HostEngine(soundfont=...)）")
    return SF2Source(soundfont, program=program, **params)
