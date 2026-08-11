import glob, os, time
from tsov.dsp.transcribe import transcribe

BASE = r"<repo>"
wavs = sorted(glob.glob(os.path.join(BASE, r"output\m1-preprocess\denoised\*.wav")))
wav = wavs[0]
print("testing:", os.path.basename(wav))
for model in ["tiny", "full"]:
    t0 = time.time()
    v = transcribe(wav, backend="crepe_notes", model=model)
    print(f"model={model}: notes={len(v.notes)} elapsed={round(time.time()-t0,1)}s")
    if v.notes:
        n = v.notes[0]
        print("  first:", round(n.start,2), round(n.end,2), n.pitch_midi, round(n.confidence,2))
