"""transport 深化（ADR-0013 里程碑二）：实时音频线程 + 传输控制。

四件套：
- `MixStreamer`：预渲染缓冲的流式切片（快路径；任何音源可用）
- `SynthStreamer`：**实时调度**——块回调里按时间推进 MIDI 事件、从流式音源（SF2）增量拉样；
  seek = 音源 reset + 事件指针快进 + 跨定位点的持续音补触发
- `Transport`：状态机（stopped/playing/paused）+ position/seek/loop；`pull(n)` 是唯一驱动入口
- `AudioStreamer`：sounddevice.OutputStream 回调驱动 `Transport.pull`（回调线程逐块合成）

离线测试不经设备：直接按块调用 `Transport.pull(n)`（纯推进，无副作用依赖）。
线程说明：流体合成在音频回调线程内跑（prototype 可接受；GIL 下 fluidsynth C 侧不阻塞）。
"""

from __future__ import annotations

import time

import numpy as np

from .session import HostSession


class MixStreamer:
    """预渲染缓冲流式切片。"""

    def __init__(self, audio: np.ndarray, samplerate: int):
        self.audio = np.asarray(audio, dtype=np.float32)
        self.samplerate = int(samplerate)
        self._pos = 0

    @property
    def duration(self) -> float:
        return len(self.audio) / self.samplerate

    def reset(self, t: float = 0.0) -> None:
        self._pos = max(0, int(round(t * self.samplerate)))

    def next_block(self, n_frames: int) -> np.ndarray:
        i0 = self._pos
        i1 = min(len(self.audio), i0 + n_frames)
        out = np.zeros((n_frames,), dtype=np.float32)
        if i1 > i0:
            out[: i1 - i0] = self.audio[i0:i1]
        self._pos = i0 + n_frames
        return out


class SynthStreamer:
    """实时调度流式器：流式音源（SF2）逐块增量合成。"""

    def __init__(self, session: HostSession, samplerate: int | None = None):
        self.session = session
        self.samplerate = int(samplerate or session.samplerate)
        self.frames = 0
        self.tracks: list[dict] = []
        for ht in session.tracks:
            src = ht.source
            if not getattr(src, "streamable", False):
                raise RuntimeError(f"音源不支持实时流式（streamable=False）：{type(src).__name__}")
            events: list[tuple[float, int, int, int]] = []
            for n in ht.notes:
                vel = max(1, min(127, int(round(127 * min(1.0, max(0.0, float(n.velocity)))))))
                events.append((float(n.start), 1, int(n.pitch_midi), vel))
                events.append((float(n.end), 0, int(n.pitch_midi), 0))
            events.sort(key=lambda e: e[0])
            self.tracks.append({
                "src": src,
                "events": events,
                "ptr": 0,
                "vol": float(getattr(ht.track.instrument, "volume", 1.0) or 1.0),
            })

    @property
    def duration(self) -> float:
        return self.session.duration + 1.0  # 尾音释放余量

    def reset(self, t: float = 0.0) -> None:
        """定位到 t 秒：音源重置 + 事件快进（跨定位点的持续音补一次 note_on）。"""
        t = max(0.0, float(t))
        self.frames = int(round(t * self.samplerate))
        for tr in self.tracks:
            src, ev = tr["src"], tr["events"]
            src.reset()
            active: dict[int, int] = {}
            i = 0
            while i < len(ev) and ev[i][0] < t:
                _, kind, pitch, vel = ev[i]
                if kind == 1:
                    active[pitch] = vel
                else:
                    active.pop(pitch, None)
                i += 1
            tr["ptr"] = i
            for pitch, vel in active.items():
                src.note_on(pitch, vel)

    def next_block(self, n_frames: int) -> np.ndarray:
        t0 = self.frames / self.samplerate
        t1 = (self.frames + n_frames) / self.samplerate
        out = np.zeros((n_frames,), dtype=np.float32)
        for tr in self.tracks:
            src, ev = tr["src"], tr["events"]
            ptr = tr["ptr"]
            pos = 0
            while ptr < len(ev) and ev[ptr][0] <= t1:
                te, kind, pitch, vel = ev[ptr]
                i = min(n_frames, max(pos, int(round((te - t0) * self.samplerate))))
                if i > pos:
                    out[pos:i] += src.pull(i - pos) * tr["vol"]
                    pos = i
                if kind == 1:
                    src.note_on(pitch, vel)
                else:
                    src.note_off(pitch)
                ptr += 1
            if pos < n_frames:
                out[pos:n_frames] += src.pull(n_frames - pos) * tr["vol"]
            tr["ptr"] = ptr
        self.frames += n_frames
        return out


class Transport:
    """传输控制：状态机 + position/seek/loop；pull 是唯一驱动入口（回调/离线通用）。"""

    def __init__(self, renderer, samplerate: int):
        self.renderer = renderer
        self.samplerate = int(samplerate)
        self.state = "stopped"  # stopped | playing | paused
        self.loop: tuple[float, float] | None = None
        self._pos_frames = 0
        self.renderer.reset(0.0)

    @property
    def position(self) -> float:
        return self._pos_frames / self.samplerate

    @property
    def duration(self) -> float:
        return float(getattr(self.renderer, "duration", 0.0))

    def play(self) -> None:
        self.state = "playing"

    def pause(self) -> None:
        if self.state == "playing":
            self.state = "paused"

    def stop(self) -> None:
        self.state = "stopped"
        self.seek(0.0)

    def seek(self, t: float) -> None:
        t = max(0.0, min(float(t), self.duration))
        self._pos_frames = int(round(t * self.samplerate))
        self.renderer.reset(t)

    def pull(self, n_frames: int) -> np.ndarray:
        """驱动一次块渲染（音频回调调用；离线测试直接调）。"""
        n_frames = int(n_frames)
        if self.state != "playing":
            return np.zeros((n_frames,), dtype=np.float32)
        block = self.renderer.next_block(n_frames)
        self._pos_frames += n_frames
        if self.loop and self.loop[1] > self.loop[0]:
            if self.position >= self.loop[1]:
                self.seek(self.loop[0])
        elif self._pos_frames >= int(self.duration * self.samplerate):
            self.state = "stopped"
        return block


class AudioStreamer:
    """sounddevice 输出流驱动（回调 → Transport.pull）。"""

    def __init__(self, transport: Transport, samplerate: int | None = None, blocksize: int = 1024):
        self.transport = transport
        self.samplerate = int(samplerate or transport.samplerate)
        self.blocksize = int(blocksize)
        self._stream = None

    def start(self) -> None:
        try:
            import sounddevice as sd
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("sounddevice 未安装") from e

        def cb(outdata, frames, time_info, status):  # noqa: ANN001  回调线程
            outdata[:, 0] = self.transport.pull(frames)

        try:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate, channels=1, blocksize=self.blocksize, callback=cb
            )
            self._stream.start()
        except Exception as e:  # noqa: BLE001  无设备等
            self._stream = None
            raise RuntimeError(f"音频流启动失败（无输出设备？）：{e}") from e

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

    def wait(self, timeout: float | None = None) -> None:
        t0 = time.time()
        while self.transport.state == "playing":
            time.sleep(0.05)
            if timeout is not None and time.time() - t0 > timeout:
                return
