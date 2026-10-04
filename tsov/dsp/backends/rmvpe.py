"""RMVPE 转录后端（RVC 生态歌声音高提取器，M4-RMVPE 定案）。

- 定位：**哼唱专用底座**（帧级 f0，八度错误率远低于 CREPE）；GAME 继续管成品歌（双后端并存）。
- RMVPE 输出帧级 f0（hop=160 @16k → 10ms/帧，Hz，0=无声）+ salience 置信度；
  音符化用 CREPE 时代的规则分割（M3-FIX 的 `_segment_frames` + `_split_on_drift` + `_frame_group_to_note`），
  **不做八度校正**——RMVPE 八度稳定；且 M3-FIX 的八度校正经光谱实测是误判（中间段本是真低音）。
- 模型：vendor/RMVPE/rmvpe_model.py（RVC 版自包含）+ rmvpe.pt 权重；模块级缓存只加载一次。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

from ...core.notes import Note, Voice
from ...core.units import hz_to_deviation_cents, hz_to_midi, midi_to_hz
from . import TranscribeBackend

_ROOT = Path(__file__).resolve().parents[3]
_RMVPE_DIR = _ROOT / "vendor" / "RMVPE"

# RMVPE 默认参数（M3-FIX 沿用：jump 0.5 / min_note 60ms / conf 0.4；RMVPE salience 中位 ~0.88）
DEFAULTS = {
    "model": None,          # None → vendor/RMVPE/rmvpe.pt
    "thred": 0.01,          # voicing 阈值（salience 峰值低于此 = 无声；RVC 默认 0.03，哼唱弱起音用低些）
    "confidence_threshold": 0.4,   # 音符分割的帧置信度阈值
    "fmin": 50.0,
    "fmax": 2006.0,
    "min_note_ms": 60.0,
    "silence_gap_ms": 150.0,
    "pitch_jump_semitones": 0.5,
    "ornament_ms": 120.0,
    "drift_semitones": 1.0,
    "drift_hold_ms": 70.0,
    "drift_stable_range": 1.0,
    "drift_stable_window": 6,
}

_MODEL_CACHE: dict = {}


def _load_model(model_path: str):
    if model_path in _MODEL_CACHE:
        return _MODEL_CACHE[model_path]
    sys.path.insert(0, str(_RMVPE_DIR))
    from rmvpe_model import RMVPE  # noqa: PLC0415 延迟导入避免包加载副作用

    device = "cuda" if __import__("torch").cuda.is_available() else "cpu"
    model = RMVPE(model_path, is_half=False, device=device)
    _MODEL_CACHE[model_path] = model
    return model


def _default_model() -> Path:
    pt = _RMVPE_DIR / "rmvpe.pt"
    if not pt.is_file():
        raise RuntimeError(f"RMVPE 权重缺失：{pt}（从 hf-mirror 下载 lj1995/VoiceConversionWebUI/rmvpe.pt）")
    return pt


class RMVPEBackend(TranscribeBackend):
    name = "rmvpe"

    def transcribe(self, audio_path: str, **params) -> Voice:
        import soundfile as sf

        cfg = {**DEFAULTS, **params}
        model_path = cfg["model"] or str(_default_model())
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise FileNotFoundError(f"音频不存在：{audio_path}")

        audio, sr = sf.read(str(audio_path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if audio.shape[0] == 0:
            return self._build_voice(audio_path, [])

        # RMVPE 的 mel 与时间轴按 16kHz 设计（hop=160 → 10ms/帧）；非 16k 输入重采样，否则时间轴错位
        if sr != 16000:
            try:
                import librosa

                audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
                sr = 16000
            except Exception as e:  # noqa: BLE001 librosa 缺失等 → 明确报错
                raise ValueError(
                    f"RMVPE 需要 16kHz 输入，当前 {sr}Hz 且重采样失败（{e}）。请先 tsov preprocess 转 16k。"
                ) from e

        model = _load_model(str(model_path))
        f0, conf = model.infer_pitch(audio, thred=float(cfg["thred"]))
        f0 = f0.astype(np.float64)
        conf = conf.astype(np.float64)
        time = np.arange(len(f0), dtype=np.float64) * (160.0 / float(sr))  # hop=160 @16k → 10ms

        frame_segs = _segment_frames(time, f0, conf, cfg)
        notes: list[Note] = []
        for t0, t1, idx in frame_segs:
            note = _frame_group_to_note(t0, t1, f0[idx], conf[idx], cfg)
            if note is not None:
                notes.append(note)
        notes.sort(key=lambda n: n.start)
        return self._build_voice(audio_path, notes)

    def _build_voice(self, audio_path: Path, notes: list[Note]) -> Voice:
        from ..transcribe import _build_voice

        return _build_voice(str(audio_path), self.name, notes)


# ---------------------------------------------------------------------------
# 规则音符分割（M3-FIX CREPE 时代恢复，见 git f5bce11^:tsov/dsp/transcribe.py）
# ---------------------------------------------------------------------------

def _segment_frames(time, pitch_hz, conf, cfg):
    """帧级音高 → 音符段列表 [(start, end, frame_indices)]。

    规则（Q25/M3-FIX）：① 置信度阈值过滤；② 停顿切分；③ 音高突变切分；
    ④ 漂移切分（legato 连音）；⑤ 短段丢弃。
    """
    conf_thr = cfg["confidence_threshold"]
    fmin, fmax = cfg["fmin"], cfg["fmax"]
    gap_s = cfg["silence_gap_ms"] / 1000.0
    jump_st = cfg["pitch_jump_semitones"]
    min_s = cfg["min_note_ms"] / 1000.0
    step_s = time[1] - time[0] if len(time) > 1 else 0.01

    voiced = np.flatnonzero((conf >= conf_thr) & (pitch_hz >= fmin) & (pitch_hz <= fmax) & (pitch_hz > 0))
    if voiced.size == 0:
        return []

    segments = []
    seg_start = voiced[0]
    prev = voiced[0]
    for i in voiced[1:]:
        gap = time[i] - time[prev]
        jump = abs(hz_to_midi(pitch_hz[i]) - hz_to_midi(pitch_hz[prev]))
        if gap > gap_s or jump > jump_st:
            segments.append((seg_start, prev + 1))
            seg_start = i
        prev = i
    segments.append((seg_start, prev + 1))

    result = []
    for a, b in segments:
        for c, d in _split_on_drift(a, b, time, pitch_hz, cfg):
            t0 = time[c]
            t1 = time[d - 1] + step_s
            if t1 - t0 < min_s:
                continue
            result.append((t0, t1, np.arange(c, d)))
    return result


def _split_on_drift(seg_start, seg_end, time, pitch_hz, cfg):
    """legato 连音漂移切分（M3-FIX）：平滑滑音帧间变化 < jump_st 不会触发跳变切分，
    用"稳定音高水平"再细分。vibrato 不误切。

    全程用段内局部索引（0..len(midi)），返回前再换算回全局索引——修复 M4 恢复时
    "全局 cur_start + 局部 boundary" 的混用（第二次及以后的切分边界会整体后移）。
    """
    step_s = time[1] - time[0] if len(time) > 1 else 0.01
    stable_win = int(cfg.get("drift_stable_window", 6))
    stable_range = float(cfg.get("drift_stable_range", 1.0))
    drift_st = float(cfg.get("drift_semitones", 1.0))
    hold_frames = max(1, int(round(float(cfg.get("drift_hold_ms", 70.0)) / 1000.0 / step_s)))
    if seg_end - seg_start < stable_win + 2:
        return [(seg_start, seg_end)]

    midi = np.array([hz_to_midi(pitch_hz[i]) for i in range(seg_start, seg_end)])
    subs = []
    cur_start = 0  # 段内局部索引（返回时统一加 seg_start 换回全局）
    anchor = float(np.median(midi[:stable_win]))
    recent = list(midi[:stable_win])
    confirm = 0
    boundary = None
    trans_start = None
    for k in range(stable_win, len(midi)):
        m = midi[k]
        recent = recent[1:] + [m]
        rng = max(recent) - min(recent)
        if rng <= stable_range:
            new_level = float(np.median(recent))
            if abs(new_level - anchor) >= drift_st:
                if confirm == 0:
                    boundary = trans_start if trans_start is not None else k - (stable_win - 1)
                confirm += 1
                if confirm >= hold_frames:
                    subs.append((cur_start, boundary))
                    cur_start = boundary
                    anchor = new_level
                    confirm = 0
                    trans_start = None
                    boundary = None
            else:
                confirm = 0
                boundary = None
                trans_start = None
                anchor = new_level
        else:
            confirm = 0
            if trans_start is None and abs(m - anchor) >= drift_st:
                trans_start = k - (stable_win - 1)
    subs.append((cur_start, len(midi)))
    return [(seg_start + a, seg_start + b) for a, b in subs]


def _frame_group_to_note(t0, t1, group_hz, group_conf, cfg):
    """一个音符段的帧 → Note：音高取中位数，置信度取均值，量化到半音 + 音分偏移。"""
    hz_med = float(np.median(group_hz))
    if not (hz_med > 0) or not np.isfinite(hz_med):
        return None
    confidence = float(np.clip(np.mean(group_conf), 0.0, 1.0))
    pitch_midi = int(round(hz_to_midi(hz_med)))
    return Note(
        start=float(t0),
        end=float(t1),
        pitch_midi=pitch_midi,
        pitch_hz=hz_med,
        velocity=confidence,
        confidence=confidence,
        deviation_cents=float(hz_to_deviation_cents(hz_med)),
        is_ornament=(float(t1) - float(t0)) < cfg["ornament_ms"] / 1000.0,
    )
