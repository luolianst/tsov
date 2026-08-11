import glob, os, time
from tsov.dsp.transcribe import transcribe

BASE = r"<repo>"
wavs = sorted(glob.glob(os.path.join(BASE, r"output\m1-preprocess\denoised\*.wav")))
print("found", len(wavs), "files")
wav = wavs[0]
print("testing:", os.path.basename(wav))
t0 = time.time()
v = transcribe(wav, backend="crepe_notes", model="tiny")
print("notes", len(v.notes), "elapsed", round(time.time() - t0, 1), "s")
if v.notes:
    n = v.notes[0]
    print("first note:", n.start, n.end, n.pitch_midi, n.confidence)
