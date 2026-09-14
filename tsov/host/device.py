"""输出设备：多轨混合 + 离线 WAV 渲染 / 实时播放（ADR-0013）。

实时策略（毛胚，防音频线程翻车）：**预渲染完整缓冲 → sounddevice 流输出**；
不在回调线程里实时调度合成（音频线程问题隔离到里程碑二）。
离线渲染与实时回放走同一 graph（mix_graph 是唯一通路）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .session import HostSession


def mix_graph(session: HostSession, samplerate: int | None = None) -> np.ndarray:
    """（兼容入口）把音轨图混合成一条 mono 音频。

    M-V4 起内部走总线化混音（host/mix.render_buses，stereo=False）——
    track → bus → master、volume/pan/mute/solo/automation；
    缺省数据（无 pan/mute/auto）下数值与旧实现逐位一致。
    - 时长 = session.duration + 1s 释放尾
    - 防削波：峰值 >1.0 时整体缩放（不 clip 削波）
    """
    from .mix import render_buses

    return render_buses(session, samplerate=samplerate, stereo=False)


def write_wav(audio: np.ndarray, path, samplerate: int = 44100) -> str:
    """音频 → WAV（PCM_16）。"""
    import soundfile as sf

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, samplerate, subtype="PCM_16")
    return str(path)


def play(audio: np.ndarray, samplerate: int = 44100, blocking: bool = True) -> None:
    """播放预渲染缓冲（sounddevice 流输出，阻塞到播完）。"""
    try:
        import sounddevice as sd
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("sounddevice 未安装（uv pip install sounddevice）；或用宿主离线渲染导出 wav") from e
    try:
        sd.play(audio.astype(np.float32), samplerate)
        if blocking:
            sd.wait()
    except Exception as e:  # pragma: no cover  无音频设备等
        raise RuntimeError(f"播放失败（无输出设备？）：{e}") from e
