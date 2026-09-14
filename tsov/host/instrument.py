"""音源适配器（ADR-0013「音源标准兼容」接口层）。

标准兼容清单：
- SF2/SF3：pyfluidsynth（`SF2Source`，实时流式渲染，非文件 midi2audio）
- SFZ：内置最小采样器（`SFZSource`，子集解析 + 线性插值变调；完整语义可走 sfizz VST3）
- VST3：pedalboard 底座（`VST3Source`，乐器插件 MIDI 渲染一次成型；popsicle 备胎未用）
- GM program：复用 `tsov/midi/export.GM_PROGRAMS`；MIDI 1.0 音符事件（start/end/pitch/velocity）直接消费

接口契约：`render(notes, samplerate, n_frames) -> mono float32 [-1,1]`。
换音源 = 新写一个 SoundSource 子类，宿主/上层零改动。
"""

from __future__ import annotations

import abc
import re
from pathlib import Path
from typing import Iterable

import numpy as np

from ..core.notes import Note
from ..midi.export import GM_PROGRAMS, is_drum_track, program_number  # noqa: F401  约定即文档


class SoundSource(abc.ABC):
    """音源接口：音符序列 → 音频缓冲。

    - 每轨一个实例（毛胚规模）；volume 由混合层（device.mix_graph）统一应用
    - 缺省输出 mono float32（多轨混音层面再按 Instrument.volume 加权）
    - `streamable=True` 的音源另支持实时增量拉流（pull/note_on/note_off/reset → transport 深化用）
    """

    name: str = "abstract"
    streamable: bool = False

    @abc.abstractmethod
    def render(self, notes: Iterable[Note], samplerate: int, n_frames: int) -> np.ndarray:
        """音符序列 → mono float32（长度 n_frames，不足补零）。"""

    def reset(self) -> None:
        """清空内部状态（实时 seek 用；默认无状态无需处理）。"""

    def close(self) -> None:
        """释放底层资源（synth/句柄），引擎 shutdown 时调用。"""


_FLUID_BLOCK = 64  # FluidSynth 内部渲染块的样本数（实测：音符事件按 64 样本边界量化）


class SF2Source(SoundSource):
    """SF2/SF3 SoundFont 音源（pyfluidsynth 实时合成，初版实现）。

    - 每个源一个 Synth 实例（get_samples 直接拉流，不走 midi2audio 文件渲染）
    - 通道策略：打击乐 → channel 9（GM percussion），其余 → channel 0
    - 多轨体积大了再共享 engine（接口已隔离，只换实现）
    - **确定性**（M-V4）：FluidSynth 事件按 64 样本块量化（onset 相位 = 累计渲染样本
      mod 64）；`reset()` 会把相位补齐到 64 的倍数 → 同一源多次渲染逐样本一致
      （导出矩阵里 probe/stems/buses 多遍渲染对齐的前提）
    """

    name = "sf2"
    streamable = True

    def __init__(
        self,
        soundfont: str,
        program: str = "piano",
        gain: float = 0.8,
        samplerate: int = 44100,
        fx: bool = False,
    ):
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
        self._synth.program_select(self._channel, self._sfid, 0, program_number(self.program))
        self._ticks = 0  # 累计已渲染样本数（相位对齐用）
        # 离线渲染确定性：FluidSynth 的 reverb/chorus 缓冲无法清的 API（system_reset 也不清），
        # 多遍渲染会互相串尾音 → 缺省关闭；需要混响走效果链层（EffectProcessor，后续）
        self.fx = bool(fx)
        if not self.fx:
            self._synth.set_reverb_level(0.0)
            self._synth.set_chorus_level(0.0)

    def render(self, notes: Iterable[Note], samplerate: int, n_frames: int) -> np.ndarray:
        notes = list(notes)
        # M-V4：渲染前清 synth 状态——保证多次渲染的确定性（导出矩阵里同一会话要渲
        # probe/stems/buses 多遍，若不清会带上一次渲染的尾音/状态 → 各产物对不上）
        self.reset()
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
        self._ticks += max(1, int(n_frames))
        buf = buf.reshape(-1, 2)
        return buf.mean(axis=1)

    # ---- 实时流式接口（transport 深化用）----

    def note_on(self, pitch: int, velocity: int) -> None:
        self._synth.noteon(self._channel, int(pitch), int(velocity))

    def note_off(self, pitch: int) -> None:
        self._synth.noteoff(self._channel, int(pitch))

    def pull(self, n_frames: int) -> np.ndarray:
        """从当前 synth 时间增量拉 n_frames 帧 → mono float32。"""
        return self._pull(n_frames)

    def reset(self) -> None:
        """system_reset + 相位对齐 + 重选 program（实时 seek / 多次渲染一致性用）。

        相位对齐：事件量化按 64 样本块，把「累计渲染样本」补到 64 的倍数后，
        后续同一事件序列的量化相位与首次渲染一致 → 输出逐样本可复现。
        """
        if not hasattr(self._synth, "system_reset"):
            raise RuntimeError("pyfluidsynth 版本无 system_reset，无法实时 seek")
        self._synth.system_reset()
        pad = (-self._ticks) % _FLUID_BLOCK
        if pad:
            self._pull(pad)
        self._synth.program_select(self._channel, self._sfid, 0, program_number(self.program))

    def close(self) -> None:
        self._synth.delete()


class VST3Source(SoundSource):
    """VST3 音源（pedalboard 底座，ADR-0013 里程碑二实现）。

    - 加载：`plugin_path` 指向 .vst3；**Windows 包（bundle 目录）形式需给到内层二进制**
      `xxx.vst3/Contents/x86_64-win/xxx.vst3`（实测 pedalboard 0.9.25：bundle 路径扫描失败、内层成功）
    - 渲染：乐器插件走 `process(midi_messages=[([bytes], t_sec)…], sample_rate, duration)` 一次成型；
      输出立体声 → mono。音色由插件 preset/参数控制（`program` 字段忽略，program 形如 "vst3:<path>"）
    """

    name = "vst3"

    def __init__(self, plugin_path: str, program: str = "", **params):
        import pedalboard  # 懒加载：没装 pedalboard 时其他音源不受影响

        self.plugin_path = str(plugin_path)
        self._plugin = pedalboard.load_plugin(self.plugin_path)
        if not getattr(self._plugin, "is_instrument", False):
            raise RuntimeError(f"VST3Source 需要乐器插件（is_instrument=False）：{plugin_path}")
        self._params = dict(params)

    def render(self, notes: Iterable[Note], samplerate: int, n_frames: int) -> np.ndarray:
        notes = list(notes)
        midi: list[tuple] = []
        for n in notes:
            vel = int(round(127.0 * min(1.0, max(0.0, float(n.velocity)))))
            vel = max(1, min(127, vel))
            t_on = max(0.0, float(n.start))
            t_off = max(t_on + 0.01, float(n.end))
            midi.append(([0x90, int(n.pitch_midi), vel], t_on))
            midi.append(([0x80, int(n.pitch_midi), 0], t_off))
        midi.sort(key=lambda m: m[1])
        duration = max(0.05, n_frames / float(samplerate))
        out = self._plugin.process(
            midi_messages=midi, sample_rate=float(samplerate), duration=float(duration)
        )
        audio = np.asarray(out, dtype=np.float32)
        if audio.ndim == 2:  # pedalboard 输出 (channels, frames)
            audio = audio.mean(axis=0)
        if audio.shape[0] < n_frames:
            audio = np.pad(audio, (0, n_frames - audio.shape[0]))
        return audio[:n_frames]


class SFZSource(SoundSource):
    """SFZ 采样器音源（内置最小实现，ADR-0013 里程碑二）。

    支持的 SFZ 子集（原型级，够用即止）：
    - `<global>` / `<group>` 段落默认值（被 `<region>` 继承）
    - region：`sample`（必填，相对 sfz 文件解析）、`lokey`/`hikey`、`pitch_keycenter`、`key`、
      `lovel`/`hivel`、`volume`（dB）、`offset`（采样偏移，Samples）
    - 渲染：线性插值变调（ratio = 2^((note-keycenter)/12)）、力度平方映射、单发（不循环）
    - 未命中任何 region 的音符 → 静音跳过（不报错）

    更完整语义（loop/滤波器/包络/多区域交叉淡入）留给 sfizz VST3 插件接 VST3Source 走。
    """

    name = "sfz"

    def __init__(self, sfz_path: str, **params):
        self.sfz_path = Path(sfz_path)
        if not self.sfz_path.is_file():
            raise RuntimeError(f"SFZ 文件不存在：{sfz_path}")
        self._regions = _parse_sfz(self.sfz_path)
        if not self._regions:
            raise RuntimeError(f"SFZ 无可用 region（子集不支持？）：{sfz_path}")
        self._cache: dict[Path, np.ndarray] = {}

    def _sample(self, rel: str) -> np.ndarray:
        p = (self.sfz_path.parent / rel.replace("\\", "/")).resolve()
        if p not in self._cache:
            import soundfile as sf

            audio, _sr = sf.read(str(p), dtype="float32", always_2d=True)
            self._cache[p] = audio.mean(axis=1)  # mono
        return self._cache[p]

    def render(self, notes: Iterable[Note], samplerate: int, n_frames: int) -> np.ndarray:
        out = np.zeros((n_frames,), dtype=np.float32)
        for n in notes:
            reg = _pick_region(self._regions, int(n.pitch_midi), float(n.velocity))
            if reg is None:
                continue
            sample = self._sample(reg["sample"])
            if reg.get("offset"):
                sample = sample[int(reg["offset"]):]
            ratio = 2.0 ** ((int(n.pitch_midi) - int(reg.get("pitch_keycenter", 60))) / 12.0)
            start = max(0.0, float(n.start))
            dur = max(0.01, float(n.end) - start)
            out_len = int(round(dur * samplerate))
            src_idx = np.arange(out_len, dtype=np.float64) * ratio
            take = sample[: int(min(len(sample), np.ceil(src_idx[-1] + 2)))] if out_len else sample[:0]
            if take.size < 2:
                continue
            vals = np.interp(src_idx, np.arange(take.size), take).astype(np.float32)
            gain = (min(1.0, max(0.0, float(n.velocity))) ** 2) * (10.0 ** (float(reg.get("volume", 0.0)) / 20.0))
            vals = vals * gain
            i0 = int(round(start * samplerate))
            i1 = min(n_frames, i0 + out_len)
            if i1 > i0:
                out[i0:i1] += vals[: i1 - i0]
        return out


# ---------------------------------------------------------------------------
# SFZ 子集解析 / region 选择（SFZSource 用）
# ---------------------------------------------------------------------------


def _sfz_value(v: str):
    v = v.strip().strip('"')
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


_OPCODE_RE = re.compile(r'(\w+)\s*=\s*("[^"]*"|[^\s"]+)')  # 一行可含多个 opcode（SFZ 常见写法）


def _parse_sfz(path: Path) -> list[dict]:
    """解析 <global>/<group>/<region> 子集；region 合并 global+group 默认值。

    兼容「`<region> sample=… lokey=…` 整行」（段头与 opcode 同行的常见写法）。
    """
    regions: list[dict] = []
    glob: dict = {}
    group: dict = {}
    scope: str | None = None
    current: dict | None = None

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("<"):
            close = line.find(">")
            tag = line[1:close].strip().lower() if close > 0 else ""
            rest = line[close + 1:].strip() if close > 0 else ""
            scope = tag if tag in ("global", "group", "region") else None
            if tag == "group":
                group = dict(glob)
            current = None
            if tag == "region":
                current = {**glob, **group}
                regions.append(current)
            line = rest           # 行内剩余继续按 opcode 解析
            if not line:
                continue
        elif scope is None:
            continue
        for m in _OPCODE_RE.finditer(line):
            k, v = m.group(1).lower(), _sfz_value(m.group(2))
            if scope == "global":
                glob[k] = v
            elif scope == "group":
                group[k] = v
            elif scope == "region" and current is not None:
                current[k] = v
    return [r for r in regions if r.get("sample")]


def _region_bounds(reg: dict) -> tuple[int, int, int, int, int]:
    """(lokey, hikey, lovel, hivel, pitch_keycenter)；`key=` 同时充当三者。"""
    key = reg.get("key")
    lokey = int(reg.get("lokey", key if key is not None else 0))
    hikey = int(reg.get("hikey", key if key is not None else 127))
    kc = int(reg.get("pitch_keycenter", key if key is not None else 60))
    lovel = int(reg.get("lovel", 1))
    hivel = int(reg.get("hivel", 127))
    return lokey, hikey, lovel, hivel, kc


def _pick_region(regions: list[dict], pitch: int, velocity: float) -> dict | None:
    vel = int(round(min(1.0, max(0.0, velocity)) * 127))
    best = None
    for reg in regions:
        lokey, hikey, lovel, hivel, _kc = _region_bounds(reg)
        if lokey <= pitch <= hikey and lovel <= vel <= hivel:
            best = reg  # 简化：后匹配者优先（不做 layer crossfade）
    return best


def make_source(program: str, soundfont: str | None = None, **params) -> SoundSource:
    """按 Instrument.program 构造音源（工厂）。

    program 形如 "vst3:<path>" / "sfz:<path>" 时走对应音源（pedalboard / 内置采样器）；
    其余按 SF2（fluidsynth）处理。
    """
    if program.startswith("vst3:"):
        return VST3Source(program[len("vst3:") :], **params)
    if program.startswith("sfz:"):
        return SFZSource(program[len("sfz:") :], **params)
    if not soundfont:
        raise RuntimeError("SF2 音源需要 soundfont 路径（make_source(soundfont=...) 或 HostEngine(soundfont=...)）")
    return SF2Source(soundfont, program=program, **params)
