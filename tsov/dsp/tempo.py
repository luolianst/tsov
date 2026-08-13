"""BPM 估计（M7 节奏）：相邻音符 onset 间隔（IOI）中位数 → bpm。

- 主法：IOI 中位数抗噪（哼唱自由节奏）；confidence = 1 - CV（IOI 越稳越高）
- 可选辅法：librosa.beat_track（需音频，成品歌更准）——audio 参数提供时交叉验证
- fallback：notes 太少/IOI 无意义时返回默认 bpm
"""

from __future__ import annotations

import numpy as np


def estimate_bpm(
    notes: list,
    fallback: float = 120.0,
    audio=None,
    sr: int = 16000,
    **params,
) -> tuple[float, float]:
    """估计 BPM。

    notes: 带 .start 的对象列表（音符）
    返回 (bpm, confidence)；confidence 0-1（IOI 稳定性）。
    """
    if len(notes) < 4:
        return float(fallback), 0.0

    onsets = np.array(sorted(float(n.start) for n in notes), dtype=np.float64)
    iois = np.diff(onsets)
    if iois.size < 3:
        return float(fallback), 0.0

    # 过滤极端间隔（<0.08s 的滑音碎片 / >2.0s 的停顿）
    valid = iois[(iois >= 0.08) & (iois <= 2.0)]
    if valid.size < 3:
        return float(fallback), 0.0

    # 拍间隔 ≈ "慢的一半"间隔的中位数（快速连音/八分音符的短间隔不算拍，
    # 取高于中位数的间隔中位数——更可能是真实拍间隔）
    med_ioi = float(np.median(valid))
    slow = valid[valid >= med_ioi]
    beat_ioi = float(np.median(slow)) if slow.size >= 2 else med_ioi
    bpm_ioi = 60.0 / beat_ioi
    # 防止过慢/过快失真
    bpm_ioi = float(np.clip(bpm_ioi, 40.0, 220.0))
    cv = float(np.std(valid) / np.mean(valid)) if np.mean(valid) > 0 else 1.0
    conf = float(np.clip(1.0 - cv, 0.0, 1.0))

    # 可选：librosa beat_track 交叉验证（成品歌更准）
    if audio is not None and params.get("use_librosa", True):
        try:
            import librosa

            tempo, _ = librosa.beat.beat_track(y=audio, sr=sr)
            bpm_lib = float(np.atleast_1d(tempo)[0])
            if 40 <= bpm_lib <= 240:
                # 两者取接近者：IOI 与 librosa 差 >30% 且 librosa 更稳（成品歌）时用 librosa
                if abs(bpm_ioi - bpm_lib) / max(bpm_ioi, 1.0) > 0.3 and conf < 0.6:
                    return bpm_lib, float(np.clip(0.6, 0.0, 1.0))
        except Exception:  # noqa: BLE001 librosa 失败不影响主法
            pass

    return bpm_ioi, round(conf, 3)
