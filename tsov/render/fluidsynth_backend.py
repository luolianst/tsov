"""fluidsynth 渲染后端（第一适配器，回放音色中性钢琴）。

M3 填充实现：Score → MIDI（pretty_midi）→ fluidsynth 合成 WAV。
- 优先 pyfluidsynth（ctypes 直调 libfluidsynth DLL），Windows 无 fluidsynth CLI 也行
- SoundFont：默认 vendor/soundfonts/FluidR3_GM.sf2（本仓库 vendor 区）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..core.score import Score
from ..midi.export import PIANO_PROGRAM, score_to_midi
from .backend import Backend

# 回放音色（ADR-0004）：中性钢琴，GM bank 0 preset 0
PIANO_PRESET = 0
PIANO_BANK = 0
SAMPLE_RATE = 44100

# 候选 SoundFont 路径（依次查找，取第一个存在的）
SOUNDFONT_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "vendor" / "soundfonts" / "FluidR3_GM.sf2",
    Path("vendor") / "soundfonts" / "FluidR3_GM.sf2",
]

# 候选 libfluidsynth DLL 目录（Windows）
DLL_DIR_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "vendor" / "fluidsynth" / "bin",
    Path("vendor") / "fluidsynth" / "bin",
    Path("C:/tools/fluidsynth/bin"),
]


def default_soundfont() -> str | None:
    """返回默认 SoundFont 路径；找不到返回 None（调用方决定降级策略）。"""
    for p in SOUNDFONT_CANDIDATES:
        if p.is_file():
            return str(p)
    return None


def _dll_dir() -> str | None:
    for p in DLL_DIR_CANDIDATES:
        if p.is_dir() and any(p.glob("libfluid*.dll")):
            return str(p)
    return None


_FLUID = None


def _load_fluidsynth():
    """加载 pyfluidsynth（模块级只注册一次）。DLL 目录加入搜索路径后 import。

    幂等（2026-09-24 热修）：原实现每次调用都向 PATH 前置 dll_dir——Web 长驻进程反复
    渲染会把 PATH 涨到 32767 上限（ValueError），之后进程环境块超限导致该进程所有
    CreateProcess 报 WinError 8（web 全瘫）。现改为模块级缓存 + PATH 不重复添加。
    """
    global _FLUID
    if _FLUID is not None:
        return _FLUID
    dll_dir = _dll_dir()
    if dll_dir and os.name == "nt":
        path = os.environ.get("PATH", "")
        if dll_dir not in path.split(os.pathsep):
            os.environ["PATH"] = dll_dir + os.pathsep + path
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(dll_dir)
    import fluidsynth

    _FLUID = fluidsynth
    return _FLUID


def render_midi(midi_path: str | Path, out_wav: str | Path, soundfont: str | None = None) -> str:
    """MIDI 文件 → 合成 WAV（pyfluidsynth file renderer）。

    - midi_path：pretty_midi 导出的 .mid
    - out_wav：输出 wav 路径
    - soundfont：.sf2 路径；默认取 vendor/soundfonts/FluidR3_GM.sf2
    - 返回 out_wav 路径；DLL/SoundFont 缺失时抛 RuntimeError（明确失败，不静默）
    """
    midi_path = Path(midi_path)
    out_wav = Path(out_wav)
    if not midi_path.is_file():
        raise RuntimeError(f"MIDI 文件不存在：{midi_path}")

    soundfont = soundfont or default_soundfont()
    if not soundfont or not Path(soundfont).is_file():
        raise RuntimeError(
            f"SoundFont 不存在：{soundfont!r}——请先下载到 vendor/soundfonts/FluidR3_GM.sf2"
        )

    fluidsynth = _load_fluidsynth()
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    fs = fluidsynth.Synth(gain=0.8, samplerate=SAMPLE_RATE)
    try:
        sfid = fs.sfload(str(soundfont))
        if sfid == fluidsynth.FLUID_FAILED:
            raise RuntimeError(f"SoundFont 加载失败：{soundfont}")
        fs.program_select(0, sfid, PIANO_BANK, PIANO_PRESET)
        fs.midi2audio(str(midi_path), str(out_wav))
    finally:
        fs.delete()

    if not out_wav.is_file():
        raise RuntimeError(f"合成未产生输出文件：{out_wav}")
    return str(out_wav)


class FluidsynthBackend(Backend):
    name = "fluidsynth"

    def render(self, score: Score, output_path: str, soundfont: str | None = None, **params) -> str:
        """Score → WAV：先导 MIDI（score_to_midi）再 fluidsynth 合成。

        中间 .mid 与 .wav 同目录同名（out.wav → out.mid 同级暂存）。
        """
        from pathlib import Path

        out_wav = Path(output_path)
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        midi_path = out_wav.with_suffix(".mid")
        score_to_midi(score, str(midi_path))
        try:
            return render_midi(midi_path, out_wav, soundfont=soundfont)
        finally:
            if midi_path.exists() and params.get("keep_midi", False) is False:
                try:
                    midi_path.unlink()
                except OSError:
                    pass
