"""音频素材音源（M-V8 E2 音频轨·第一刀）：工程音频文件 → 时间线摆样。

- ``AudioClipSource``：``render(notes, samplerate, n_frames)`` → (n,) float32——
  文件样本按 ``offset`` 秒摆放进目标缓冲；文件 SR ≠ 目标 SR → ``resample_to`` 兜底重采样
  （入库已统一 44.1k，本路径为兼容/测试用）。
- ``streamable = False``：不参与实时流式通道（SynthStreamer 会拒绝 → 自动回退 MixStreamer）；
  播放走**渲染混入**（任务书 §9.1 C：一处集成，试听/宿主播放/导出全通）。
- 第一刀不做读缓存：每次 render 直读文件（渲染本身秒级，音频读 ~百 ms 属噪声级；按需再加）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def load_mono(path, samplerate: int | None = None) -> tuple[np.ndarray, int]:
    """读音频文件 → (mono float32, 实际 SR)；``samplerate`` 给定时重采样到该 SR。"""
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    if samplerate is not None and int(sr) != int(samplerate):
        mono = resample_to(mono, int(sr), int(samplerate))
        sr = samplerate
    return mono, int(sr)


def resample_to(audio: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """重采样（scipy resample_poly 优先；scipy 缺失时线性插值兜底）。"""
    audio = np.asarray(audio, dtype=np.float32)
    if sr_from == sr_to or audio.size == 0:
        return audio
    try:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(sr_from), int(sr_to))
        return resample_poly(audio, sr_to // g, sr_from // g).astype(np.float32)
    except Exception:  # noqa: BLE001 —— scipy 缺失/异常：线性兜底
        n_out = max(1, int(round(len(audio) * sr_to / sr_from)))
        x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, audio).astype(np.float32)


class AudioClipSource:
    """工程音频文件 → 时间线样本（单 clip/轨）。"""

    streamable = False

    def __init__(self, path, *, offset: float = 0.0, samplerate: int = 44100):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"音频文件不存在：{self.path}")
        self.offset = max(0.0, float(offset or 0.0))
        import soundfile as sf

        info = sf.info(str(self.path))
        self.file_samplerate = int(info.samplerate)
        self.channels = int(info.channels)
        self.clip_duration = (float(info.frames) / self.file_samplerate) if self.file_samplerate else 0.0
        self.samplerate = int(samplerate)  # 期待的目标 SR（渲染引擎）；render 以实参为准

    @property
    def clip_end(self) -> float:
        """该 clip 在时间线上的结束秒（= offset + 文件时长）。"""
        return self.offset + self.clip_duration

    def render(self, notes, samplerate: int, n_frames: int) -> np.ndarray:  # noqa: ARG002 —— 音频轨无音符，notes 不参与
        """按 offset 摆放文件样本 → (n_frames,) float32。"""
        mono, _ = load_mono(self.path, samplerate=int(samplerate))
        out = np.zeros((int(n_frames),), dtype=np.float32)
        i0 = int(round(self.offset * int(samplerate)))
        if i0 >= n_frames:
            return out
        seg = mono[: max(0, n_frames - i0)]
        out[i0 : i0 + len(seg)] = seg
        return out

    def close(self) -> None:  # 与 SoundSource 协议一致（无资源可放）
        return None
