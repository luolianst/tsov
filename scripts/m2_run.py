"""M2 对比跑批：人声参考谱对比（3 首 × 双底座）+ 哼唱自足指标（6 首 × 双底座）。

产物：
  output/m2-compare/<slug>/<backend>/stage-voice.json + report.json + report.md   （人声轨）
  output/m2-compare/humming/<backend>/<song>/stage-voice.json + report.json       （哼唱轨）
  output/m2-compare/COMPARE-REPORT.md                                             （汇总）

流程（人声轨）：转码16k → noisereduce 降噪 → 双底座转录 → 主旋律提取 → compare_to_reference
流程（哼唱轨）：输入已降噪 wav → 双底座转录 → compute_self_contained
"""
import glob
import json
import os
import time

BASE = r"<repo>"
RAW = os.path.join(BASE, "samples", "raw", "纯人声音乐")
MIDI = os.path.join(BASE, "samples", "reference", "midi")
OUT = os.path.join(BASE, "output", "m2-compare")
HUM = os.path.join(BASE, "output", "m1-preprocess", "denoised")
BACKENDS = ("crepe_notes", "basic-pitch")

# crepe 模型容量：full 在 TF-CPU 约 10x 实时（14.7s→142s），批量用 tiny（约 0.5x 实时）；
# full 抽一首做对照（见 COMPARE-REPORT.md）。可用 --model full 覆盖。
CREPE_MODEL = "tiny"


def _crepe_model():
    import sys

    if "--model" in sys.argv:
        i = sys.argv.index("--model")
        return sys.argv[i + 1]
    return CREPE_MODEL

VOCAL_TRIALS = [
    {
        "slug": "sound-of-silence",
        "pattern": "*Sound of Silence*",
        "midi": "sound-of-silence-melody.mid",
    },
    {
        "slug": "will-the-circle",
        "pattern": "*Will the Circle*",
        "midi": "will-the-circle-melody.mid",
    },
    {
        "slug": "canon",
        "pattern": "*Canon*",
        "midi": "pachelbel-canon-satb-melody.mid",
    },
]


def log(msg):
    print(msg, flush=True)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def run_ffmpeg(inp, outp, sr=16000):
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", inp,
         "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", outp],
        check=True,
        capture_output=True,
    )


def denoise(inp, outp, strength=0.7):
    import noisereduce as nr
    import soundfile as sf

    audio, sr = sf.read(inp)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    reduced = nr.reduce_noise(y=audio, sr=sr, stationary=True, prop_decrease=strength)
    sf.write(outp, reduced, sr)


def run_vocal(trial):
    from tsov.dsp.transcribe import transcribe
    from tsov.eval.reference import compare_to_reference, load_reference_notes

    slug = trial["slug"]
    matches = glob.glob(os.path.join(RAW, trial["pattern"]))
    if not matches:
        log(f"SKIP {slug}: no vocal file")
        return None
    vocal = matches[0]
    midi_path = os.path.join(MIDI, trial["midi"])
    if not os.path.exists(midi_path):
        log(f"SKIP {slug}: missing {trial['midi']}")
        return None

    outdir = os.path.join(OUT, slug)
    os.makedirs(outdir, exist_ok=True)

    # 转码+降噪（幂等：已有产物直接复用）
    audio_dir = os.path.join(outdir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    wav16 = os.path.join(audio_dir, "16k.wav")
    denoised = os.path.join(audio_dir, "denoised.wav")
    if not os.path.exists(denoised):
        if not os.path.exists(wav16):
            run_ffmpeg(vocal, wav16)
        denoise(wav16, denoised)
    input_wav = denoised

    ref_notes = load_reference_notes(midi_path)
    write_json(os.path.join(outdir, "notes", "reference.json"), [n.to_dict() for n in ref_notes])

    row = {"slug": slug, "vocal": os.path.basename(vocal), "midi": trial["midi"], "backends": {}}
    for backend in BACKENDS:
        try:
            t0 = time.time()
            voice = transcribe(input_wav, backend=backend, model=_crepe_model())
            elapsed = round(time.time() - t0, 1)
            metrics = compare_to_reference(voice, midi_path)  # 内部做主旋律提取

            bdir = os.path.join(outdir, backend)
            write_json(os.path.join(bdir, "stage-voice.json"), voice.to_dict())
            report = {
                "vocal": os.path.basename(vocal),
                "midi": trial["midi"],
                "backend": backend,
                "elapsed_sec": elapsed,
                "ref_notes": metrics.detail["ref_notes"],
                "hyp_notes_raw": metrics.detail["hyp_notes_raw"],
                "hyp_notes_melody": metrics.detail["hyp_notes_after_melody"],
                "precision": metrics.precision,
                "recall": metrics.recall,
                "f1": metrics.f1,
                "onset_error_ms": metrics.onset_error_ms,
                "params": {"onset_window_s": 0.10, "pitch_tol": 0, "melody_extraction": True},
            }
            write_json(os.path.join(bdir, "report.json"), report)
            write_json(os.path.join(bdir, "report.md"), _vocal_md(report))
            row["backends"][backend] = report
            log(f"VOCAL {slug}/{backend}: notes_raw={report['hyp_notes_raw']} melody={report['hyp_notes_melody']} "
                f"P={report['precision']:.3f} R={report['recall']:.3f} F1={report['f1']:.3f} ({elapsed}s)")
        except Exception as e:  # noqa: BLE001 单底座失败不中断
            log(f"ERROR {slug}/{backend}: {e}")
            row["backends"][backend] = {"error": str(e)}

    write_json(os.path.join(outdir, "report.json"), row)
    return row


def _vocal_md(r) -> str:
    return f"""# 参考谱对比 — {r['vocal']}

- 参考 MIDI：{r['midi']}
- 底座：{r['backend']}（耗时 {r['elapsed_sec']}s）
- 对比参数：onset ±100ms、音高容差 0 半音、先主旋律提取

| 指标 | 值 |
|------|-----|
| 参考音符数 | {r['ref_notes']} |
| 转录音符数（原始） | {r['hyp_notes_raw']} |
| 转录音符数（主旋律提取后） | {r['hyp_notes_melody']} |
| 精确率 Precision | {r['precision']:.4f} |
| 召回率 Recall | {r['recall']:.4f} |
| F1 | {r['f1']:.4f} |
| onset 对齐误差 | {r['onset_error_ms']:.1f} ms |
"""


def run_humming(wav, backend):
    from tsov.dsp.transcribe import transcribe
    from tsov.eval.self_contained import compute_self_contained

    stem = os.path.splitext(os.path.basename(wav))[0]
    t0 = time.time()
    voice = transcribe(wav, backend=backend, model=_crepe_model())
    elapsed = round(time.time() - t0, 1)
    metrics = compute_self_contained(voice)

    bdir = os.path.join(OUT, "humming", backend)
    write_json(os.path.join(bdir, f"{stem}.json"), {
        "source": os.path.basename(wav),
        "backend": backend,
        "elapsed_sec": elapsed,
        "notes": len(voice.notes),
        "self_contained": metrics.to_dict(),
    })
    write_json(os.path.join(bdir, f"{stem}.voice.json"), voice.to_dict())
    log(f"HUM {stem}/{backend}: notes={len(voice.notes)} "
        f"cents={metrics.pitch_accuracy['mean_cents']} "
        f"conf={metrics.confidence['mean']} ({elapsed}s)")
    return {
        "song": stem,
        "backend": backend,
        "elapsed_sec": elapsed,
        "notes": len(voice.notes),
        "self_contained": metrics.to_dict(),
    }


def main():
    import sys

    os.makedirs(OUT, exist_ok=True)
    args = sys.argv[1:]
    only_vocal = "--only-vocal" in args
    only_humming = "--only-humming" in args
    slug_filter = None
    if "--slug" in args:
        slug_filter = args[args.index("--slug") + 1]

    # 人声轨
    vocal_rows = []
    if not only_humming:
        for t in VOCAL_TRIALS:
            if slug_filter and t["slug"] != slug_filter:
                continue
            row = run_vocal(t)
            if row:
                vocal_rows.append(row)

    # 哼唱轨
    humming_rows = []
    if not only_vocal:
        humming_files = sorted(glob.glob(os.path.join(HUM, "*.wav")))
        for wav in humming_files:
            for backend in BACKENDS:
                try:
                    humming_rows.append(run_humming(wav, backend))
                except Exception as e:  # noqa: BLE001 单首失败不中断全批
                    log(f"ERROR humming {os.path.basename(wav)}/{backend}: {e}")

    # M2-RUN7：--only-vocal / --only-humming 分开跑时不能互相清空对方的汇总 json，
    # 只写本次实际跑过的轨，另一轨保持磁盘上已有产物不动。
    if not only_humming:
        write_json(os.path.join(OUT, "vocal-results.json"), vocal_rows)
    if not only_vocal:
        write_json(os.path.join(OUT, "humming-results.json"), humming_rows)
    log(f"DONE vocal={len(vocal_rows)} humming={len(humming_rows)}")


if __name__ == "__main__":
    main()
