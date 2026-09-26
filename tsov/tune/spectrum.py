"""粗频谱小工具（M-V8 E4 段2 · Q2）：每轨 4–6 频段能量占比 + 谱心（纯 numpy，零状态）。

口径：
- 分帧（n_fft=4096，hop=半窗；帧数封顶 max_frames=1024——长音频均匀抽帧控时耗）
- rfft → 功率谱 → 频段积分 → 占比（6 段，和=1；只数 20–20000 Hz 范围）
- 空/静音缓冲 → 全 0（测量器对未出声的轨要稳，不报错）

用途：事实包的「粗频谱」——确定性通道（声像碰撞等）与 LLM 通道（模式识别型）的共同证据。
"""

from __future__ import annotations

import numpy as np

BANDS: tuple[tuple[float, float, str], ...] = (
    (20.0, 120.0, "低"),
    (120.0, 350.0, "中低"),
    (350.0, 1200.0, "中"),
    (1200.0, 4000.0, "中高"),
    (4000.0, 10000.0, "高"),
    (10000.0, 20000.0, "超高"),
)
N_FFT = 4096
MAX_FRAMES = 1024
MIN_SAMPLES = 256


def mono(audio) -> np.ndarray:
    """多声道 → 单声道（均值）；一维原样。"""
    a = np.asarray(audio, dtype=np.float64)
    if a.ndim == 2:
        # 声道维总是较小的一维（帧×声道 / 声道×帧 两种排布均兼容）
        a = a.mean(axis=0 if a.shape[0] < a.shape[1] else 1)
    return a.ravel()


def _power_frames(a: np.ndarray, samplerate: int, n_fft: int, max_frames: int):
    """分帧 → (freqs, 平均功率谱)；样本太少 → None。"""
    if a.size < MIN_SAMPLES:
        return None
    n_fft = max(MIN_SAMPLES, int(min(n_fft, 2 ** int(np.log2(max(a.size, MIN_SAMPLES))))))
    hop = max(1, n_fft // 2)
    view = np.lib.stride_tricks.sliding_window_view(a, n_fft)[::hop]
    if view.shape[0] == 0:
        view = a[-n_fft:][None, :]
    if view.shape[0] > max_frames:
        idx = np.linspace(0, view.shape[0] - 1, max_frames).astype(int)
        view = view[idx]
    win = np.hanning(n_fft)
    spec = np.fft.rfft(view * win, axis=1)
    power = (np.abs(spec) ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / float(samplerate))
    return freqs, power


def band_shares(audio, samplerate: int, *, n_fft: int = N_FFT,
                max_frames: int = MAX_FRAMES) -> list[float]:
    """6 频段能量占比（和≈1；空/静音 → 全 0）。"""
    a = mono(audio)
    got = _power_frames(a, samplerate, n_fft, max_frames)
    if got is None:
        return [0.0] * len(BANDS)
    freqs, power = got
    cap = min(20000.0, float(samplerate) / 2.0)
    energies: list[float] = []
    total = 0.0
    for lo, hi, _ in BANDS:
        sel = (freqs >= lo) & (freqs < min(hi, cap))
        e = float(power[sel].sum()) if sel.any() else 0.0
        energies.append(e)
        total += e
    if total <= 0:
        return [0.0] * len(BANDS)
    return [round(e / total, 4) for e in energies]


def spectral_centroid(audio, samplerate: int, *, n_fft: int = N_FFT,
                      max_frames: int = MAX_FRAMES) -> float:
    """谱心（Hz；能量加权频率重心）；空/静音 → 0.0。"""
    a = mono(audio)
    got = _power_frames(a, samplerate, n_fft, max_frames)
    if got is None:
        return 0.0
    freqs, power = got
    sel = (freqs >= 20.0) & (freqs <= 20000.0)
    p = power[sel]
    f = freqs[sel]
    s = float(p.sum())
    return round(float((f * p).sum() / s), 1) if s > 0 else 0.0


def cosine(a, b) -> float:
    """两支频谱占比的余弦相似度（声像碰撞判据）；任一为空 → 0.0。"""
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na <= 0 or nb <= 0 or va.shape != vb.shape:
        return 0.0
    return round(float(np.dot(va, vb) / (na * nb)), 4)
