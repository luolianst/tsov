# Optional engines — setup guide

tsov ships **without** the large third-party engines (size + licensing). This
repo still does a lot on its own: the web workspace, score editing, mixing,
MIDI import/export and the agent chat all work with no engines installed — the
engines add **transcription**, **audio rendering**, and **VST3 instrument/effect
hosting** (demo-tested with the two plugins below).

| Component | Unlocks | Download | Expected location |
|---|---|---|---|
| FluidSynth 2.x (DLLs) | MIDI → audio rendering & playback | ~5 MB | `vendor/fluidsynth/bin/` |
| `FluidR3_GM.sf2` | GM instrument soundbank | ~148 MB | `vendor/soundfonts/` |
| [RMVPE](https://github.com/Dream-High/RMVPE) + `rmvpe.pt` | Humming → notes | ~185 MB | `vendor/RMVPE/` |
| [GAME](https://github.com/openvpi/GAME) + weights | Produced vocals → notes | ~290 MB | `vendor/GAME/` |
| [Dexed](https://github.com/asb2m10/dexed) + [TAL-Chorus-LX](https://tal-software.com/products/tal-chorus-lx) | VST3 instrument & effect support (demo pair) | ~18 MB | `vendor/vst3/` |

---

## 1. Quick path — the fetch script

```bash
# from the repository root
python scripts/fetch_engines.py                     # fetch everything + verify
python scripts/fetch_engines.py --check             # just probe the download sources
python scripts/fetch_engines.py --only rmvpe vst3   # pick components
python scripts/fetch_engines.py --no-verify         # skip the functional checks
```

The script downloads and places everything below, sets up GAME's virtualenv
(reusing the tsov venv's torch — see §2d), and finishes with quick functional
checks (FluidSynth load, RMVPE/GAME wiring, VST3 plugin load). It is
idempotent — existing files are kept (`--force` to re-fetch).

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

# the upstream Dream-High repo does NOT contain rmvpe_model.py — it's an
# RVC-derived single-file adaptation maintained in this repo (see its header):
cp scripts/vendor-extras/rmvpe_model.py vendor/RMVPE/rmvpe_model.py

# weights (181 MB) — official:
curl -L -o vendor/RMVPE/rmvpe.pt \
  https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt
# China mirror equivalent:
# https://hf-mirror.com/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt
```

tsov loads `vendor/RMVPE/rmvpe_model.py` + `rmvpe.pt` directly (no separate install).

### 2d. GAME (produced vocals) — the fiddly one

GAME runs as a subprocess in **its own venv** that reuses tsov's torch/numpy/scipy
via a `.pth` file plus a **shared-package prune** (GAME must not shadow the shared
numeric stack — see the `SHARED_PRUNE` list in `scripts/fetch_engines.py`; a
duplicated numpy/scipy pair breaks imports). Reproduce exactly:

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

# 5) prune duplicated shared packages so torch/numpy/scipy & co. resolve from the tsov venv
#    (numpy/scipy/torch/librosa/… — full list = SHARED_PRUNE in scripts/fetch_engines.py):
uv pip uninstall --python vendor/GAME/.venv/Scripts/python.exe numpy scipy torch numba llvmlite librosa soundfile sympy matplotlib mido tqdm h5py resampy tensorboard h5py requests certifi cffi six packaging …
#    (simplest: just run `python scripts/fetch_engines.py --only game` and let it do this)

# 6) wire GAME's venv to the tsov venv site-packages so it can see torch:
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

### 2e. VST3 test plugins (Dexed + TAL-Chorus-LX)

tsov hosts VST3 instruments & effects through **pedalboard**. The demo-tested
pair (a synth and a chorus effect):

- **Dexed** (DX7 FM synth, GPL-3.0) — instrument:
  <https://github.com/asb2m10/dexed/releases/download/v1.0.1/Dexed-1.0.1-win.zip>
- **TAL-Chorus-LX** (Juno-60 chorus, freeware) — effect:
  <https://tal-software.com/downloads/plugins/install_TAL-Chorus-LX.zip>

Both zips contain ready-to-use `*.vst3` bundles — extract them into `vendor/vst3/`:

```
vendor/vst3/Dexed.vst3/...
vendor/vst3/TAL-Chorus-LX.vst3/...
```

(These are Windows builds; on macOS/Linux install your own VST3 builds or skip.)

**Important:** on Windows, pedalboard wants the **inner binary** path of the
bundle — not the bundle directory:

```
vendor/vst3/Dexed.vst3/Contents/x86_64-win/Dexed.vst3
vendor/vst3/TAL-Chorus-LX.vst3/Contents/x86_64-win/TAL-Chorus-LX.vst3
```

Usage: bind an instrument with `program = "vst3:<inner path>"`; add an effect of
kind `vst3` with `path = "<inner path>"`. The web UI's prompts prefill the TAL
path for effects.

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

# 4) VST3 test plugins (instrument + effect load via pedalboard)
tsov/.venv/Scripts/python.exe -c "from pedalboard import load_plugin; d=load_plugin('vendor/vst3/Dexed.vst3/Contents/x86_64-win/Dexed.vst3'); t=load_plugin('vendor/vst3/TAL-Chorus-LX.vst3/Contents/x86_64-win/TAL-Chorus-LX.vst3'); print('vst3 ok:', d.is_instrument, t.is_instrument)"
```

`python scripts/fetch_engines.py` runs a lighter version of these checks
automatically after fetching (`--no-verify` to skip).

In the web UI, a working setup means: **Render** plays, the humming
quick-lane's transcription step produces MIDI, and a `vst3:` instrument /
effect loads without errors.

## 4. Troubleshooting

| Symptom | Fix |
|---|---|
| GitHub / HuggingFace unreachable | `--gh-mirror https://ghfast.top --hf-mirror https://hf-mirror.com`, or curl with your own proxy |
| `GAME 未安装` / `RMVPE 权重缺失` | the engine isn't in `vendor/` — re-run the fetch script or §2c/§2d |
| GAME fails importing numpy/scipy (`np.long`, `_ckdtree`) | the shared-package prune was skipped — GAME's venv shadows the tsov stack; re-run `python scripts/fetch_engines.py` (or uninstall the `SHARED_PRUNE` packages from GAME's venv) |
| GAME device patch fails to apply | you're on a different GAME revision — pin the tested commit (§2d step 1) or skip the patch (device flag simply won't be available) |
| GAME crashes with encoding errors on Windows | set `PYTHONUTF8=1` (tsov does this for its own subprocess calls; needed when invoking GAME's `infer.py` by hand) |
| VST3 plugin fails to load | give the **inner binary** path (`xxx.vst3/Contents/x86_64-win/xxx.vst3`) — pedalboard does not scan bundle directories on Windows |
| `uv sync` removed torch | reinstall per §2b; avoid bare `uv sync` in this repo |
| First transcription is slow | model load: RMVPE ~10 s, GAME ~20 s; later calls reuse the loaded model |

## 5. Licenses & credit

None of these are redistributed by this repo — fetch them from their upstream
projects, under their terms:

- [FluidSynth](https://github.com/FluidSynth/fluidsynth) — LGPL-2.1+
- FluidR3_GM soundfont — see [pianobooster/fluid-soundfont](https://github.com/pianobooster/fluid-soundfont) (Frank Wen's FluidR3)
- [RMVPE](https://github.com/Dream-High/RMVPE) — Apache-2.0 · weights hosted by the RVC project (`lj1995/VoiceConversionWebUI`)
- [GAME](https://github.com/openvpi/GAME) — MIT (openvpi)
- [Dexed](https://github.com/asb2m10/dexed) — GPL-3.0 (asb2m10)
- [TAL-Chorus-LX](https://tal-software.com/products/tal-chorus-lx) — freeware, see TAL's EULA
