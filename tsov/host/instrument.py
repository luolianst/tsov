"""音源适配器（ADR-0013「音源标准兼容」接口层）。

标准兼容清单：
- SF2/SF3：pyfluidsynth（`SF2Source`，实时流式渲染，非文件 midi2audio）
- SFZ：内置最小采样器（`SFZSource`，子集解析 + 线性插值变调；完整语义可走 sfizz VST3）
- VST3：DawDreamer 底座（`VST3Source`，批L L1-1 换底；bundle 目录或内层二进制均可；离线一次成型渲染）
- GM program：复用 `tsov/midi/export.GM_PROGRAMS`；MIDI 1.0 音符事件（start/end/pitch/velocity）直接消费

接口契约：`render(notes, samplerate, n_frames) -> mono float32 [-1,1]`。
换音源 = 新写一个 SoundSource 子类，宿主/上层零改动。
"""

from __future__ import annotations

import abc
import os
import re
import threading
from pathlib import Path
from typing import Iterable

import numpy as np

from ..core.notes import Note
from ..midi.export import DRUM_BANK, GM_PROGRAMS, is_drum_track, program_number  # noqa: F401  约定即文档
from .vst_worker import VST_BLOCK, apply_params, events_from_notes, finish_audio, get_worker


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


# ---- P0 B1：SF2 Synth 池（跨会话复用已加载 SoundFont）----
# 键 = (soundfont 绝对路径, program, gain, samplerate, fx)；close() 归池而非 delete。
# ⚠️ S5 定案：**默认关闭**。FluidSynth 的 system_reset 不清内部振荡器/效果缓冲，
# 复用池化 synth 会让同工程两次导出出现可闻/可测差异（实测 diff 0.20）。
# 需要性能时用 TSOV_SF2_POOL=1 显式开启，但该模式不承诺逐样本确定性。
_SF2_POOL: dict[tuple, list["SF2Source"]] = {}
_SF2_POOL_LOCK = threading.Lock()
_SF2_POOL_MAX = 3           # 每键最多留 3 个（并发渲染上限；再多直接销毁）


def _sf2_pool_enabled() -> bool:
    return os.environ.get("TSOV_SF2_POOL") == "1"


def _sf2_key(soundfont, program, gain, samplerate, fx) -> tuple:
    try:
        sf = str(Path(soundfont).resolve())
    except Exception:  # noqa: BLE001 路径异常时退回原串（仅作键）
        sf = str(soundfont)
    return (sf, str(program), round(float(gain), 6), int(samplerate), bool(fx))


def _sf2_take(key: tuple) -> "SF2Source | None":
    if not _sf2_pool_enabled():
        return None
    with _SF2_POOL_LOCK:
        lst = _SF2_POOL.get(key)
        if lst:
            return lst.pop()
    return None


def _sf2_put(key: tuple, src: "SF2Source") -> bool:
    if not _sf2_pool_enabled():
        return False
    with _SF2_POOL_LOCK:
        lst = _SF2_POOL.setdefault(key, [])
        if len(lst) >= _SF2_POOL_MAX:
            return False
        lst.append(src)
        return True


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
        self._pool_key = _sf2_key(soundfont, program, gain, samplerate, fx)
        self._closed = False
        pooled = _sf2_take(self._pool_key)
        if pooled is not None:
            self._synth = pooled._synth
            self._sfid = pooled._sfid
            self._synth.system_reset()   # 清上次会话状态（尾音/参数）
        else:
            self._synth = self._fluid.Synth(gain=gain, samplerate=self._samplerate)
            self._sfid = self._synth.sfload(str(soundfont))
            if self._sfid == self._fluid.FLUID_FAILED:
                self._synth.delete()
                raise RuntimeError(f"SoundFont 加载失败：{soundfont}")
        self.program = program
        self._channel = 9 if is_drum_track(program) else 0
        self._select_program()
        self._ticks = 0  # 累计已渲染样本数（相位对齐用）
        # 离线渲染确定性：FluidSynth 的 reverb/chorus 缓冲无法清的 API（system_reset 也不清），
        # 多遍渲染会互相串尾音 → 缺省关闭；需要混响走效果链层（EffectProcessor，后续）
        self.fx = bool(fx)
        if not self.fx:
            self._synth.set_reverb_level(0.0)
            self._synth.set_chorus_level(0.0)

    def _select_program(self) -> None:
        """选预设：打击乐 → GM 鼓组（bank 128 / preset 0 Standard Kit）；其余 → bank 0。

        bug 教训（2026-09-27）：鼓轨曾走 `program_select(ch9, bank=0, preset=0)`——
        bank0/preset0 即大钢琴，把通道 9 的默认鼓组顶掉 →「内置 drum 实为钢琴」。
        """
        if is_drum_track(self.program):
            self._synth.program_select(self._channel, self._sfid, DRUM_BANK, 0)
        else:
            self._synth.program_select(self._channel, self._sfid, 0, program_number(self.program))

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
        self._select_program()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        key = getattr(self, "_pool_key", None)
        if key is not None and _sf2_put(key, self):
            return   # 归池复用（下次同键会话直接用，免 sfload）
        self._synth.delete()


_PRESET_KINDS = ("vstpreset", "fxp", "state")


def _normalize_preset(preset: dict | None, base_dir) -> dict | None:
    """预设引用规范化：校验 kind/path，相对路径按工程根（base_dir）解析为绝对。

    （worker 侧 cwd 不保证 = 调用方 cwd，故统一在构造期定型为绝对路径。）
    """
    if not preset:
        return None
    kind = str(preset.get("kind") or "")
    path = str(preset.get("path") or "").strip()
    if kind not in _PRESET_KINDS or not path:
        raise RuntimeError(f"预设引用非法：{preset!r}（需 kind∈{_PRESET_KINDS} + path）")
    p = Path(path)
    if not p.is_absolute():
        base = Path(base_dir) if base_dir else Path.cwd()
        p = (base / p).resolve()
    return {"kind": kind, "path": str(p)}


class VST3Source(SoundSource):
    """VST3 音源（DawDreamer 底座——批L L1-1 换底 / L1-2 走常驻 worker，ADR-0025）。

    - 底座模式：默认经**常驻 worker 子进程**加载/渲染（崩溃隔离；生命周期见 `vst_worker.py`，
      `tsov web` 服务退出时收尾）。`TSOV_VST_INPROC=1` 时退回进程内直载（排障/应急，不隔离）
    - 加载：`plugin_path` 指向 .vst3 **bundle 目录**或**内层二进制**均可（dawdreamer 0.9.0 实测）
    - 乐器判定：**不以输入通道数为据**（实测 Dexed 0/2、sfizz/Vital 0/2，但 Surge XT 是 2/2 合成器——
      与 TAL-Chorus 效果器 2/2 无法按通道区分）——一律放行；效果器误接表现为渲染全零，
      排障见 worker `status`（in/out 通道）与渲染 peak
    - 渲染：`add_midi_note`（秒单位）灌事件 → 离线一次成型；输出立体声 → mono；延迟补偿后归位
    - 多遍确定性：渲染前 `clear_midi` + 重灌事件（回确定起点）；起音残渣/收敛微尘实测口径见
      `output/v02-batchL-verify/`（起音窗内 ≤1e-4；窗外 ≤1e-6，典型为 0）
    - 参数/预设（批L L1-3/L1-4）：`params` = Instrument 参数 diff（{名/下标: 归一 0..1}，严格校验、
      失配报错）；`preset` = 预设引用（vstpreset/fxp/state）——渲染前「载预设 → 应用 diff → clear_midi」
      回确定起点（worker 侧每遍重设；进程内直载在载入后一次性应用）
    """

    name = "vst3"

    def __init__(self, plugin_path: str, program: str = "", params: dict | None = None,
                 preset: dict | None = None, base_dir=None, **kwargs):
        self.plugin_path = str(plugin_path)
        self._samplerate = int(kwargs.pop("samplerate", 44100) or 44100)
        self._params = {str(k): float(v) for k, v in (params or {}).items()}  # 参数 diff（归一 0..1）
        self._preset = _normalize_preset(preset, base_dir)  # 预设引用（路径已定型为绝对）
        self._engine = self._proc = self._daw = None
        self._param_names = None
        self._latency = 0
        self._worker = get_worker()  # None = 进程内直载（TSOV_VST_INPROC=1）
        if self._worker is not None:
            self._worker.load(self.plugin_path, sr=self._samplerate)  # 失败 → RuntimeError
            return
        import dawdreamer as daw  # 懒加载：没装 dawdreamer 时其他音源不受影响

        self._daw = daw
        self._load(self._samplerate)

    # ---- 进程内直载（应急路径；同规则的另一实现见 vst_worker._ChildState）----

    def _load(self, samplerate: int) -> None:
        """建引擎 + 载插件 + 乐器校验 + 应用构造参数（失败抛 RuntimeError，不静默）。"""
        engine = self._daw.RenderEngine(int(samplerate), VST_BLOCK)
        try:
            proc = engine.make_plugin_processor("instrument", self.plugin_path)
        except Exception as exc:
            raise RuntimeError(f"VST3 加载失败：{self.plugin_path}（{exc}）") from exc
        # 输入通道数不可作乐器判定依据（Surge XT 2/2 是合成器；TAL-Chorus 2/2 是效果器）——
        # 一律放行；效果器误接表现为渲染全零，见 worker status（in/out）与渲染 peak
        self._engine, self._proc = engine, proc
        self._samplerate = int(samplerate)
        self._latency = int(proc.get_latency_samples())
        if self._preset:  # 预设 = 基座（先载），参数 diff 叠加其后
            try:
                self._apply_preset_inproc(proc)
            except Exception as exc:
                raise RuntimeError(f"预设加载失败：{self._preset['path']!r}（{exc}）") from exc
        try:
            self._param_names = apply_params(proc, self._params, self._param_names, strict=True)
        except ValueError as exc:
            raise RuntimeError(f"VST3 参数失配：{self.plugin_path}（{exc}）") from exc

    def _apply_preset_inproc(self, proc) -> None:
        kind, path = self._preset["kind"], self._preset["path"]
        if kind == "vstpreset":
            proc.load_vst3_preset(path)
        elif kind == "fxp":
            proc.load_preset(path)
        else:
            proc.load_state(path)

    def render(self, notes: Iterable[Note], samplerate: int, n_frames: int) -> np.ndarray:
        samplerate = int(samplerate)
        if self._worker is not None:
            return self._worker.render(
                self.plugin_path, events_from_notes(notes), samplerate, n_frames,
                params=self._params, preset=self._preset,
            )
        if self._engine is None or samplerate != self._samplerate:
            self._load(samplerate)  # 采样率变化 → 重建引擎（插件重载）
        proc = self._proc
        proc.clear_midi()  # 上一遍渲染的音符不残留（多遍/交叉重渲确定性）
        for pitch, vel, t_on, dur in events_from_notes(notes):
            proc.add_midi_note(int(pitch), int(vel), float(t_on), float(dur))
        duration = max(0.05, (n_frames + self._latency) / float(samplerate))
        try:
            self._engine.load_graph([(proc, [])])
            self._engine.render(duration)
            audio = self._engine.get_audio()
        except Exception as exc:
            raise RuntimeError(f"VST3 渲染失败：{self.plugin_path}（{exc}）") from exc
        return finish_audio(audio, n_frames, self._latency)

    def close(self) -> None:
        """释放：worker 模式空操作（插件常驻 worker 缓存，生命周期归 worker 管理）；
        进程内模式丢弃引擎/插件引用（GC 回收）。"""
        if self._worker is not None:
            return
        self._engine = None
        self._proc = None


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
        # 预检：至少一条 region 的采样文件可读；否则试听只会得到误解的静音/底层错误。
        if not any(self._region_sample_path(r).is_file() for r in self._regions):
            raise RuntimeError(f"SFZ 采样文件全部缺失（检查解压/relink）：{sfz_path}")
        self._cache: dict[Path, np.ndarray] = {}

    def _region_sample_path(self, reg: dict) -> Path:
        rel = str(reg.get("sample") or "").replace("\\", "/")
        return (self.sfz_path.parent / rel).resolve()

    def _sample(self, rel: str) -> np.ndarray:
        p = (self.sfz_path.parent / rel.replace("\\", "/")).resolve()
        if not p.is_file():
            raise RuntimeError(f"SFZ 采样文件缺失：{p}（SFZ: {self.sfz_path}）")
        if p not in self._cache:
            import soundfile as sf

            try:
                audio, _sr = sf.read(str(p), dtype="float32", always_2d=True)
            except Exception as exc:  # noqa: BLE001 —— 转成可读的试听错误
                raise RuntimeError(f"SFZ 采样不可读：{p}（{exc}）") from exc
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
            ratio = 2.0 ** ((int(n.pitch_midi) - _sfz_key(reg.get("pitch_keycenter", 60))) / 12.0)
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


_NOTE_NAME_RE = re.compile(r'^([a-gA-G])([#b]?)(-?\d+)$')
_NOTE_PC = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def _sfz_key(value) -> int:
    """SFZ 音高值 → MIDI：支持 0..127 整数与 `c4` / `c#4` / `db4` 音名。"""
    if isinstance(value, bool):
        raise ValueError(f"非法 SFZ 音高：{value!r}")
    if isinstance(value, int):
        midi = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"非法 SFZ 音高：{value!r}")
        midi = int(value)
    else:
        text = str(value).strip()
        try:
            midi = int(text)
        except ValueError:
            m = _NOTE_NAME_RE.match(text)
            if not m:
                raise ValueError(f"非法 SFZ 音高：{value!r}") from None
            letter, accidental, octave = m.group(1).lower(), m.group(2), int(m.group(3))
            midi = (octave + 1) * 12 + _NOTE_PC[letter]
            if accidental == "#":
                midi += 1
            elif accidental == "b":
                midi -= 1
    if not 0 <= midi <= 127:
        raise ValueError(f"SFZ 音高越界：{value!r}")
    return midi


def _region_bounds(reg: dict) -> tuple[int, int, int, int, int]:
    """(lokey, hikey, lovel, hivel, pitch_keycenter)；`key=` 同时充当三者。

    SFZ 允许音名（VPO 的 `lokey=c2` 等）；非法 region 由 `_pick_region` 跳过，
    不让整次试听因个别坏 region 失败。
    """
    key = reg.get("key")
    lokey = _sfz_key(reg.get("lokey", key if key is not None else 0))
    hikey = _sfz_key(reg.get("hikey", key if key is not None else 127))
    kc = _sfz_key(reg.get("pitch_keycenter", key if key is not None else 60))
    lovel = int(reg.get("lovel", 1))
    hivel = int(reg.get("hivel", 127))
    return lokey, hikey, lovel, hivel, kc


def _pick_region(regions: list[dict], pitch: int, velocity: float) -> dict | None:
    vel = int(round(min(1.0, max(0.0, velocity)) * 127))
    best = None
    for reg in regions:
        try:
            lokey, hikey, lovel, hivel, _kc = _region_bounds(reg)
        except (TypeError, ValueError):
            continue
        if lokey <= pitch <= hikey and lovel <= vel <= hivel:
            best = reg  # 简化：后匹配者优先（不做 layer crossfade）
    return best


def make_source(program: str, soundfont: str | None = None, **params) -> SoundSource:
    """按 Instrument.program 构造音源（工厂）。

    program 形如 "vst3:<path>" / "sfz:<path>" 时走对应音源（DawDreamer / 内置采样器）；
    其余按 SF2（fluidsynth）处理。
    """
    if program.startswith("vst3:"):
        return VST3Source(program[len("vst3:") :], **params)
    if program.startswith("sfz:"):
        return SFZSource(program[len("sfz:") :], **params)
    if not soundfont:
        raise RuntimeError("SF2 音源需要 soundfont 路径（make_source(soundfont=...) 或 HostEngine(soundfont=...)）")
    return SF2Source(soundfont, program=program, **params)
