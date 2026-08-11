"""M2 trial run: vocal mp3 -> 16k wav -> denoise -> basic-pitch transcribe -> compare vs melody.mid

Usage:
  python trial-run.py <vocal.mp3> <melody.mid> <outdir> [--no-denoise]

Outputs (in outdir):
  audio/16k.wav, audio/denoised.wav
  notes/transcribed.json    (basic-pitch note events)
  notes/reference.json      (melody notes from MIDI)
  report.json               (metrics: precision/recall/F1 + per-note alignment)
  report.md
"""
import sys, os, json, subprocess, shutil
import soundfile as sf
import numpy as np

OUT_WIN = 0.10      # onset window seconds
PITCH_TOL = 0       # semitones tolerance (0 = exact MIDI pitch match)


def run_ffmpeg(inp, outp, sr=16000):
    subprocess.run(["ffmpeg", "-y", "-i", inp, "-ac", "1", "-ar", str(sr),
                    "-c:a", "pcm_s16le", outp], check=True, capture_output=True)


def denoise(inp, outp, strength=0.7):
    import noisereduce as nr
    audio, sr = sf.read(inp)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    reduced = nr.reduce_noise(y=audio, sr=sr, stationary=True, prop_decrease=strength)
    sf.write(outp, reduced, sr)


def transcribe_basic_pitch(wav_path):
    from basic_pitch.inference import predict
    _, midi_data, _ = predict(wav_path)
    notes = []
    for inst in midi_data.instruments:
        for n in inst.notes:
            notes.append({"start": float(n.start), "end": float(n.end),
                          "pitch": int(n.pitch), "confidence": float(n.velocity) / 127.0})
    notes.sort(key=lambda x: x["start"])
    return notes


def load_melody_midi(mid_path):
    import pretty_midi
    pm = pretty_midi.PrettyMIDI(mid_path)
    notes = []
    for inst in pm.instruments:
        for n in inst.notes:
            notes.append({"start": float(n.start), "end": float(n.end),
                          "pitch": int(n.pitch), "velocity": int(n.velocity)})
    notes.sort(key=lambda x: x["start"])
    return notes


def align(ref_notes, hyp_notes):
    """Greedy matching: for each ref note, find best hyp note within onset window + pitch tol."""
    used = set()
    tp = 0
    alignments = []
    for r in ref_notes:
        best = None
        for i, h in enumerate(hyp_notes):
            if i in used:
                continue
            if abs(h["start"] - r["start"]) <= OUT_WIN and abs(h["pitch"] - r["pitch"]) <= PITCH_TOL:
                if best is None or abs(h["start"] - r["start"]) < abs(best["start"] - r["start"]):
                    best = (i, h)
        if best:
            used.add(best[0])
            tp += 1
            alignments.append({"ref_start": r["start"], "ref_pitch": r["pitch"],
                               "hyp_start": best[1]["start"], "hyp_pitch": best[1]["pitch"]})
    return tp, alignments


def main():
    vocal, midi, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    do_denoise = "--no-denoise" not in sys.argv
    os.makedirs(os.path.join(outdir, "audio"), exist_ok=True)
    os.makedirs(os.path.join(outdir, "notes"), exist_ok=True)

    # 1. transcode
    wav16 = os.path.join(outdir, "audio", "16k.wav")
    run_ffmpeg(vocal, wav16)
    # 2. denoise
    input_wav = wav16
    if do_denoise:
        denoised = os.path.join(outdir, "audio", "denoised.wav")
        denoise(wav16, denoised)
        input_wav = denoised
    # 3. transcribe
    hyp = transcribe_basic_pitch(input_wav)
    ref = load_melody_midi(midi)
    # 4. compare
    tp, alignments = align(ref, hyp)
    precision = tp / len(hyp) if hyp else 0.0
    recall = tp / len(ref) if ref else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    report = {
        "vocal": os.path.basename(vocal),
        "midi": os.path.basename(midi),
        "audio": {"input_wav": input_wav, "denoised": do_denoise},
        "ref_notes": len(ref), "hyp_notes": len(hyp),
        "tp": tp,
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "params": {"onset_window_s": OUT_WIN, "pitch_tol_semitones": PITCH_TOL},
        "alignments": alignments[:200],
    }
    with open(os.path.join(outdir, "notes", "transcribed.json"), "w", encoding="utf-8") as f:
        json.dump(hyp, f, ensure_ascii=False, indent=1)
    with open(os.path.join(outdir, "notes", "reference.json"), "w", encoding="utf-8") as f:
        json.dump(ref, f, ensure_ascii=False, indent=1)
    with open(os.path.join(outdir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    md = f"""# M2 试跑报告 — {os.path.basename(vocal)}

- 参考 MIDI：{os.path.basename(midi)}
- 输入音频：{os.path.basename(vocal)}（16k 单声道{' + noisereduce 降噪' if do_denoise else ''}）
- 转录后端：basic-pitch
- 对比参数：onset 容差 ±{int(OUT_WIN*1000)}ms，音高容差 {PITCH_TOL} 半音

| 指标 | 值 |
|------|-----|
| 参考音符数 | {len(ref)} |
| 转录音符数 | {len(hyp)} |
| 正确匹配 (TP) | {tp} |
| 精确率 Precision | {precision:.3f} |
| 召回率 Recall | {recall:.3f} |
| F1 | {f1:.3f} |

## 说明
- 精确率 = TP / 转录音符数；召回率 = TP / 参考音符数
- 音符对齐：参考音符 onset ± {int(OUT_WIN*1000)}ms 且音高一致
- 转录含装饰音/伴奏残留时精确率会偏低；清唱节奏松散时召回率会偏低
"""
    with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(f"DONE vocal={os.path.basename(vocal)} ref={len(ref)} hyp={len(hyp)} tp={tp} "
          f"P={precision:.3f} R={recall:.3f} F1={f1:.3f}")


if __name__ == "__main__":
    main()
