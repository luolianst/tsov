# Optional engines — setup guide

tsov ships **without** the large third-party engines (size + licensing). This
repo still does a lot on its own: the web workspace, score editing, mixing,
MIDI import/export and the agent chat all work with no engines installed — the
engines add **transcription** and **audio rendering**.

| Engine | Unlocks | Download | Expected location |
|---|---|---|---|
| FluidSynth 2.x (DLLs) | MIDI → audio rendering & playback | ~5 MB | `vendor/fluidsynth/bin/` |
| `FluidR3_GM.sf2` | GM instrument soundbank | ~148 MB | `vendor/soundfonts/` |
| [RMVPE](https://github.com/Dream-High/RMVPE) + `rmvpe.pt` | Humming → notes | ~185 MB | `vendor/RMVPE/` |
| [GAME](https://github.com/openvpi/GAME) + weights | Produced vocals → notes | ~290 MB | `vendor/GAME/` |

---

## 1. Quick path — the fetch script

```bash
# from the repository root
python scripts/fetch_engines.py                 # fetch everything
python scripts/fetch_engines.py --check         # just probe the download sources
python scripts/fetch_engines.py --only rmvpe game   # pick engines
```

The script downloads and places everything below, sets up GAME's virtualenv
(reusing the tsov venv's torch — see §2b), and applies our GAME device patch.
It is idempotent — existing files are kept (`--force` to re-fetch).

Requires `git` + Python 3.9+. GAME additionally needs [`uv`](https://docs.astral.sh/uv/)
and the tsov venv (`tsov/.venv` — see the README quickstart).

**China mirrors** (or let it auto-fallback for `rmvpe.pt`):

```bash
python scripts/fetch_engines.py --gh-mirror https://ghfast.top --hf-mirror https://hf-mirror.com
```

---

## 2. Manual setup (what the script does, for reference)

### 2a. FluidSynth + soundfont

**Windows.** Download the latest `win10-x64` zip from
[FluidSynth releases](https://github.com/FluidSynth/fluidsynth/releases)
(tested: **2.5.7**) and copy these DLLs from the zip's `bin\` into `vendor/fluidsynth/bin/`:

```
libfluidsynth-3.dll   SDL3.dll   sndfile.dll        (+ fluidsynth.exe, optional)
```

Then put a GM soundfont at `vendor/soundfonts/FluidR3_GM.sf2` (the app looks
for that exact name; or pass `--soundfont` on `tsov render`):

- [pianobooster/fluid-soundfont v3.1 release](https://github.com/pianobooster/fluid-soundfont/releases/tag/v3.1) (script default, 148 MB)
- [urish/cinto raw mirror](https://github.com/urish/cinto/raw/master/media/FluidR3%20GM.sf2) (fallback)

**macOS / Linux:** `brew install fluid-synth` or `apt install fluidsynth` —
pyfluidsynth finds the system library; you only need the soundfont.

### 2b. torch into the tsov venv (needed by RMVPE & GAME)

tsov itself doesn't depend on torch; the transcription engines do. One-time:

```bash
# NVIDIA GPU (tested: torch 2.9.1+cu128 on RTX 50-series):
uv pip install --python tsov/.venv torch --index-url https://download.pytorch.org/whl/cu128
# CPU-only:
uv pip install --python tsov/.venv torch
```

Note: don't run a bare `uv sync` afterwards — it would remove the
manually-installed torch (see the warning block in `pyproject.toml`).

### 2c. RMVPE (humming)

```bash
git clone https://github.com/Dream-High/RMVPE vendor/RMVPE
git -C vendor/RMVPE checkout a6db1cd7d26014aa739383367afd9bab57fc624c   # tested revision

# weights (181 MB) — official:
curl -L -o vendor/RMVPE/rmvpe.pt \
  https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt
# China mirror equivalent:
# https://hf-mirror.com/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt
```

tsov loads `vendor/RMVPE/rmvpe_model.py` + `rmvpe.pt` directly (no separate install).

### 2d. GAME (produced vocals) — the fiddly one

GAME runs as a subprocess in **its own venv** that reuses tsov's torch via a
`.pth` file. Reproduce exactly:

```bash
# 1) repo @ tested revision (2026-06-14)
git clone https://github.com/openvpi/GAME vendor/GAME
git -C vendor/GAME checkout 4ad815c90dfe2442730f3fdc866fd23e737cbc97

# 2) weights — GAME v1.0.0 "medium" (184 MB): model.pt + config.yaml + lang_map.json
#    download https://github.com/openvpi/GAME/releases/download/v1.0.0/GAME-1.0-medium.zip
#    (China: prefix with https://ghfast.top/) and extract the three files into vendor/GAME/pretrained/

# 3) our device patch — adds --device / accelerator pass-through (without it, GAME ignores the device flag; tsov degrades gracefully):
git -C vendor/GAME apply scripts/patches/game-device-flag.patch

# 4) venv from the tsov venv's python, with system-site-packages (tested layout):
tsov/.venv/Scripts/python.exe -m venv --system-site-packages vendor/GAME/.venv
uv pip install --python vendor/GAME/.venv/Scripts/python.exe -r vendor/GAME/requirements.txt
#    (torch is intentionally NOT in GAME's requirements — it comes from tsov's venv)

# 5) wire GAME's venv to the tsov venv site-packages so it can see torch:
#    (write the absolute path; on Windows minds the backslashes)
python - <<'PY'
from pathlib import Path
repo = Path.cwd()
p = repo / "vendor/GAME/.venv/Lib/site-packages/tsovvenv.pth"
p.write_text(str(repo / "tsov/.venv/Lib/site-packages") + "\n", encoding="utf-8")
print("wrote", p)
PY
```

macOS/Linux: same flow with `bin/python` and `lib/python3.x/site-packages`
(untested — feedback welcome).

---

## 3. Verify

```bash
# 1) backend registry
tsov/.venv/Scripts/tsov.exe backends
#   -> DSP backends: game, rmvpe
#   -> Render backends: ['fluidsynth']

# 2) transcription smoke test (any hummed clip; preprocess writes a 16 kHz wav under <out>/denoised/)
tsov/.venv/Scripts/tsov.exe preprocess "samples/raw/哼唱/<your clip>.m4a" -o /tmp/pre --sr 16000
tsov/.venv/Scripts/tsov.exe transcribe "/tmp/pre/denoised/<your clip>.wav" --backend rmvpe -o /tmp/rmvpe.json
#   -> /tmp/rmvpe.json contains "notes": [...]; first load takes ~10-20 s
tsov/.venv/Scripts/tsov.exe transcribe "/tmp/pre/denoised/<your clip>.wav" --backend game  -o /tmp/game.json
#   -> GAME loads its model in ~20 s, then transcribes on GPU when available

# 3) rendering smoke test (needs FluidSynth + soundfont)
tsov/.venv/Scripts/tsov.exe render <some score.json> -o /tmp/out.wav
```

In the web UI, a working setup means: **Render** plays, and the humming
quick-lane's transcription step produces MIDI.

## 4. Troubleshooting

| Symptom | Fix |
|---|---|
| GitHub / HuggingFace unreachable | `--gh-mirror https://ghfast.top --hf-mirror https://hf-mirror.com`, or curl with your own proxy |
| `GAME 未安装` / `RMVPE 权重缺失` | the engine isn't in `vendor/` — re-run the fetch script or §2c/§2d |
| GAME device patch fails to apply | you're on a different GAME revision — pin the tested commit (§2d step 1) or skip the patch (device flag simply won't be available) |
| GAME crashes with encoding errors on Windows | set `PYTHONUTF8=1` (tsov does this for its own subprocess calls; needed when invoking GAME's `infer.py` by hand) |
| `uv sync` removed torch | reinstall per §2b; avoid bare `uv sync` in this repo |
| First transcription is slow | model load: RMVPE ~10 s, GAME ~20 s; later calls reuse the loaded model |

## 5. Licenses & credit

None of these are redistributed by this repo — fetch them from their upstream
projects, under their terms:

- [FluidSynth](https://github.com/FluidSynth/fluidsynth) — LGPL-2.1+
- FluidR3_GM soundfont — see [pianobooster/fluid-soundfont](https://github.com/pianobooster/fluid-soundfont) (Frank Wen's FluidR3)
- [RMVPE](https://github.com/Dream-High/RMVPE) — Apache-2.0 · weights hosted by the RVC project (`lj1995/VoiceConversionWebUI`)
- [GAME](https://github.com/openvpi/GAME) — MIT (openvpi)
