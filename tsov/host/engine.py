"""HostEngine：宿主引擎（ADR-0013）。

入口四件事：
- `load(score)` / `load_score(score_path)` → HostSession（音轨图）
- `render(session, out_wav)` → 离线渲染（同一 graph）
- `play(session)` / `play_wav(wav)` → 实时回放（预渲染缓冲 + sounddevice）
- `stream(session)` / `play_stream(session)` → **实时流式**（回调线程逐块合成；
  SF2 → SynthStreamer 实时调度，其他音源 → MixStreamer 缓冲切片；transport 深化在 host/transport.py）

transport 控制（play/pause/stop/seek/loop）在 `host/transport.py` 的 Transport 上；
对外控制协议（daw-cli 9 动词 + 标准错误码）后续接 agentloop 工具时再包一层。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..core.score import Score
from ..render.fluidsynth_backend import default_soundfont
from .device import mix_graph, play as play_buffer, write_wav
from .session import HostSession


class HostEngine:
    """宿主引擎：Score ↔ 音轨图 ↔ 音频（离线/实时同一通路）。"""

    def __init__(self, soundfont: str | None = None, samplerate: int = 44100):
        self.soundfont = soundfont or default_soundfont()
        if not self.soundfont:
            raise RuntimeError("未找到 SoundFont——先下载到 vendor/soundfonts/FluidR3_GM.sf2（见 docs/ADR-0008）")
        self.samplerate = int(samplerate)

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def load(self, score: Score) -> HostSession:
        return HostSession.from_score(score, soundfont=self.soundfont, samplerate=self.samplerate)

    def load_score(self, score_path) -> HostSession:
        import json

        score = Score.from_dict(json.loads(Path(score_path).read_text(encoding="utf-8")))
        return self.load(score)

    # ------------------------------------------------------------------
    # 离线渲染
    # ------------------------------------------------------------------

    def render(self, session: HostSession, out_wav=None, stereo: bool = False,
               cache=None, stats: dict | None = None, on_progress=None) -> np.ndarray:
        """音轨图 → 音频（默认 mono；stereo=True 走总线混音立体声）；out_wav 给定时同时写 WAV。

        - `cache`（ADR-0018）：轨道级 freeze 库（命中即读、未命中渲后入库）；stats/on_progress 供进度上报
        """
        from .mix import render_buses

        audio = render_buses(session, samplerate=self.samplerate, stereo=stereo,
                             cache=cache, stats=stats, on_progress=on_progress)
        if out_wav:
            write_wav(audio, out_wav, samplerate=self.samplerate)
        return audio

    def export(self, session: HostSession, out_dir, **opts) -> dict:
        """导出矩阵（M-V4）：master/bus/stems WAV + MIDI；返回报告 dict。"""
        from .export import export_matrix

        return export_matrix(session, out_dir, samplerate=self.samplerate, **opts)

    def render_score(self, score_path, out_wav) -> str:
        """Score JSON → WAV（一步式，agent 工具/CLI 常用）。"""
        session = self.load_score(score_path)
        try:
            self.render(session, out_wav=out_wav)
        finally:
            session.close()
        return str(out_wav)

    # ------------------------------------------------------------------
    # 实时回放
    # ------------------------------------------------------------------

    def play(self, session: HostSession, blocking: bool = True) -> None:
        audio = mix_graph(session, samplerate=self.samplerate)
        play_buffer(audio, samplerate=self.samplerate, blocking=blocking)

    def play_score(self, score_path, blocking: bool = True) -> None:
        session = self.load_score(score_path)
        try:
            self.play(session, blocking=blocking)
        finally:
            session.close()

    def play_wav(self, wav_path, blocking: bool = True) -> None:
        import soundfile as sf

        audio, sr = sf.read(str(wav_path), dtype="float32")
        play_buffer(audio, samplerate=sr, blocking=blocking)

    # ------------------------------------------------------------------
    # 实时流式回放（transport 深化，里程碑二）
    # ------------------------------------------------------------------

    def stream(self, session: HostSession, blocksize: int = 1024):
        """构造实时音频流（含 Transport）：SF2 → SynthStreamer；其他音源 → MixStreamer 切片。"""
        from .transport import AudioStreamer, MixStreamer, SynthStreamer, Transport

        try:
            renderer = SynthStreamer(session, self.samplerate)
        except RuntimeError:
            renderer = MixStreamer(mix_graph(session, samplerate=self.samplerate), self.samplerate)
        transport = Transport(renderer, self.samplerate)
        return AudioStreamer(transport, self.samplerate, blocksize=blocksize)

    def play_stream(self, session: HostSession, blocking: bool = True) -> None:
        """实时流式播放（音频回调线程逐块合成——预渲染缓冲 → 实时调度）。"""
        streamer = self.stream(session)
        streamer.transport.play()
        streamer.start()
        try:
            if blocking:
                streamer.wait()
        finally:
            streamer.stop()
