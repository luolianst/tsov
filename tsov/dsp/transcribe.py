"""DSP 转录入口：哼唱 wav → Voice（ADR-0004 两层 schema 的原始层）。

M2 双底座（ADR-0007 定案，M2-RUN5 修正）：
- basic-pitch：多声部转录（能转和弦/多音，ONNX 推理），notes 直接来自 PrettyMIDI
- crepe_notes：CREPE 帧级音高估计（TF 版 vendor/crepe，本地权重）+ 规则启发式音符分割量化（Q25）
  （torchcrepe 对真实人声恒 flat 已弃用——M2-RUN1~4 四轮未破，洛怜拍板走 TF vendor 路线）

输出统一 ADR-0005 Voice / Note schema（字段冻结，不许改）。
"""

from __future__ import annotations

import numpy as np

from ..core.notes import Note, Voice
from .pitch import hz_to_deviation_cents, hz_to_midi, midi_to_hz
from .segment import split_into_segments

BACKENDS = ("crepe_notes", "basic-pitch")

# crepe_notes 默认参数（Q25 定案：规则启发式，N 可调）
CREPE_DEFAULTS = {
    "confidence_threshold": 0.5,   # 帧置信度低于此值视为无声/噪声
    "fmin": 50.0,                  # 音高下限 Hz（人声最低）
    "fmax": 2006.0,                # 音高上限 Hz（crepe 模型可估上限）
    "min_note_ms": 120.0,          # 短于此的段直接丢弃（噪声片段）
    "silence_gap_ms": 150.0,       # 停顿 > N ms 切分音符
    "pitch_jump_semitones": 0.9,   # 帧间音高突变超过该值（半音）切分
    "model": "full",               # crepe 模型容量（full/tiny/small/medium/large）
    "step_size_ms": 10.0,          # 帧步长
    "ornament_ms": 120.0,          # 短于此标记为装饰音
}


def transcribe(audio_path: str, backend: str = "crepe_notes", **params) -> Voice:
    """哼唱 → 结构化声部对象（原始层）。

    - backend：crepe_notes / basic-pitch（M2 双验证）
    - 输出：Note（start/end/pitch_midi/pitch_hz/velocity/confidence/deviation_cents/is_ornament）
      + bpm / bpm_confidence / segments / source_audio / backend
    """
    if backend not in BACKENDS:
        raise ValueError(f"未知 backend：{backend!r}（可选 {BACKENDS}）")
    if backend == "basic-pitch":
        return _transcribe_basic_pitch(audio_path, **params)
    return _transcribe_crepe_notes(audio_path, **params)


# ---------------------------------------------------------------------------
# basic-pitch 后端
# ---------------------------------------------------------------------------

def _transcribe_basic_pitch(audio_path: str, **params) -> Voice:
    from basic_pitch.inference import predict

    # 试跑已验证：返回 (model_output, midi_data, note_events)；用 midi_data（PrettyMIDI）提取最稳
    # M2-RUN7：装了 TF 后 basic_pitch 会优先尝试加载 TF saved model（nmp 目录）失败（ICASSP_2022_MODEL_PATH
    # 指向无扩展名的 nmp，Model.__init__ 先试 tf.saved_model.load 再一路 fallback，最终 nmp 目录抛错）；
    # 直接硬编码 nmp.onnx 绝对路径走 ONNX 推理（ADR-0007 定案路线）。
    onnx_path = r"tsov\.venv\Lib\site-packages\basic_pitch\saved_models\icassp_2022\nmp.onnx"
    _, midi_data, _ = predict(audio_path, model_or_model_path=onnx_path)
    ornament_ms = params.get("ornament_ms", CREPE_DEFAULTS["ornament_ms"]) / 1000.0
    notes = []
    for inst in midi_data.instruments:
        for n in inst.notes:
            start, end = float(n.start), float(n.end)
            pitch_midi = int(n.pitch)
            velocity = max(0.0, min(1.0, float(n.velocity) / 127.0))
            notes.append(
                Note(
                    start=start,
                    end=end,
                    pitch_midi=pitch_midi,
                    pitch_hz=midi_to_hz(pitch_midi),
                    velocity=velocity,
                    confidence=velocity,
                    deviation_cents=0.0,  # basic-pitch 输出已量化，无原始音分信息
                    is_ornament=(end - start) < ornament_ms,
                )
            )
    notes.sort(key=lambda n: n.start)
    return _build_voice(audio_path, "basic-pitch", notes)


# ---------------------------------------------------------------------------
# crepe_notes 后端：帧级音高 + 音符分割量化
# ---------------------------------------------------------------------------

def _transcribe_crepe_notes(audio_path: str, **params) -> Voice:
    import soundfile as sf
    import crepe

    cfg = {**CREPE_DEFAULTS, **params}

    audio, sr = sf.read(audio_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    if audio.shape[0] == 0:
        return _build_voice(audio_path, "crepe_notes", [])

    # TF 版 crepe：waveform 直接喂（内部 resample 到 16k），返回 (time, frequency, confidence, activation)
    time, pitch_hz, conf, _ = crepe.predict(
        audio,
        sr,
        model_capacity=cfg["model"],
        viterbi=True,
        center=True,
        step_size=cfg["step_size_ms"],
        verbose=0,
    )
    time = time.astype(np.float64)
    pitch_hz = pitch_hz.astype(np.float64)
    conf = conf.astype(np.float64)

    frame_segs = _segment_frames(time, pitch_hz, conf, cfg)
    notes = []
    for t0, t1, idx in frame_segs:
        note = _frame_group_to_note(t0, t1, pitch_hz[idx], conf[idx], cfg)
        if note is not None:
            notes.append(note)
    notes.sort(key=lambda n: n.start)
    return _build_voice(audio_path, "crepe_notes", notes)


def _segment_frames(time, pitch_hz, conf, cfg):
    """帧级音高 → 音符段列表 [(start, end, frame_indices)]。

    规则（Q25）：① 置信度阈值过滤；② 停顿切分（帧间隔 >N ms 视为新音符）；
    ③ 音高突变切分（相邻活跃帧音高跳变 >N 半音）；④ 短段丢弃（min_note_ms）。
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

    segments = []  # (start_frame_idx, end_frame_idx_exclusive)
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
        t0 = time[a]
        t1 = time[b - 1] + step_s  # 结尾补一帧步长
        if t1 - t0 < min_s:
            continue
        result.append((t0, t1, np.arange(a, b)))
    return result


def _frame_group_to_note(t0, t1, group_hz, group_conf, cfg):
    """一个音符段的帧 → Note：音高取中位数，置信度取均值，量化到半音 + 记录音分偏移。"""
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
        velocity=confidence,  # 无力度信息，置信度代偿
        confidence=confidence,
        deviation_cents=float(hz_to_deviation_cents(hz_med)),
        is_ornament=(float(t1) - float(t0)) < cfg["ornament_ms"] / 1000.0,
    )


# ---------------------------------------------------------------------------
# Voice 组装
# ---------------------------------------------------------------------------

def _build_voice(audio_path: str, backend: str, notes: list[Note]) -> Voice:
    segments = split_into_segments(notes)
    return Voice(
        notes=notes,
        bpm=120.0,
        bpm_confidence=0.0,
        segments=segments,
        source_audio=audio_path,
        backend=backend,
    )
