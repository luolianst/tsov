"""M2 trial batch: run trial-run.py on all vocals that have reference MIDI.

Chinese paths live in this file (scp transfers bytes, no shell escaping issues).
"""
import glob, os, subprocess, sys, json

BASE = r"<repo>"
RAW = os.path.join(BASE, "samples", "raw", "纯人声音乐")
MIDI = os.path.join(BASE, "samples", "reference", "midi")
OUT = os.path.join(BASE, "output", "m2-trial")
PY = os.path.join(BASE, "tsov", ".venv", "Scripts", "python.exe")
SCRIPT = os.path.join(BASE, "scripts", "trial-run.py")

TRIALS = [
    {
        "pattern": "*Sound of Silence*",
        "midi": "sound-of-silence-melody.mid",
        "slug": "sound-of-silence",
    },
    {
        "pattern": "*Will the Circle*",
        "midi": "will-the-circle-melody.mid",
        "slug": "will-the-circle",
    },
    {
        "pattern": "白金*Canon*",
        "midi": "pachelbel-canon-satb-melody.mid",
        "slug": "canon",
    },
]

results = []
for t in TRIALS:
    matches = glob.glob(os.path.join(RAW, t["pattern"]))
    if not matches:
        print(f"SKIP {t['slug']}: no vocal file matching {t['pattern']}")
        continue
    vocal = matches[0]
    midi_path = os.path.join(MIDI, t["midi"])
    if not os.path.exists(midi_path):
        print(f"SKIP {t['slug']}: missing {t['midi']}")
        continue
    outdir = os.path.join(OUT, t["slug"])
    os.makedirs(outdir, exist_ok=True)
    print(f"RUN {t['slug']}: {os.path.basename(vocal)} -> {t['midi']}")
    r = subprocess.run([PY, "-X", "utf8", SCRIPT, vocal, midi_path, outdir],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout.strip())
    if r.returncode != 0:
        print("STDERR:", r.stderr[-800:])
        results.append({"slug": t["slug"], "error": r.stderr[-500:]})
    else:
        with open(os.path.join(outdir, "report.json"), encoding="utf-8") as f:
            results.append(json.load(f))

summary = os.path.join(OUT, "summary.json")
with open(summary, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("SUMMARY ->", summary)
